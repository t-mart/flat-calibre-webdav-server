"""Flat filename generation.

The naming scheme is a contract shared with the KOReader client plugin:

    with series:     {author_sort} - {series} {index} - {title}.{ext}
    without series:  {author_sort} - {title}.{ext}

Everything here is pure: no filesystem, no database. Names must be legal on
FAT32 (the Kobo's internal storage) while preserving non-ASCII intact.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .config import ILLEGAL_FILENAME_CHARS

# Characters FAT32 forbids at the very end of a name. A dot or space mid-name is
# fine; a trailing one makes the write fail on-device.
_TRAILING_JUNK = " ."

# If the fixed parts of a name leave less room than this for the title, we stop
# trying to preserve the structure and hard-truncate the whole stem instead.
_MIN_TITLE_BUDGET = 8


@dataclass(frozen=True, slots=True)
class BookNaming:
    """The naming-relevant fields of one book, straight out of metadata.db."""

    book_id: int
    author_sort: str
    title: str
    extension: str
    series: str | None = None
    series_index: float = 1.0


def utf16_length(text: str) -> int:
    """Length in UTF-16 code units, which is what FAT32 actually counts."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def truncate_utf16(text: str, limit: int) -> str:
    """Truncate to at most `limit` UTF-16 units without splitting a character."""
    if limit <= 0:
        return ""
    used = 0
    kept: list[str] = []
    for char in text:
        cost = 2 if ord(char) > 0xFFFF else 1
        if used + cost > limit:
            break
        kept.append(char)
        used += cost
    return "".join(kept)


def _is_illegal(char: str) -> bool:
    """Illegal on FAT32, or a control character that has no business in a name."""
    return char in ILLEGAL_FILENAME_CHARS or ord(char) < 0x20 or ord(char) == 0x7F


def sanitize_component(text: str, replacement: str = "_") -> str:
    """Make one name component FAT32-legal, leaving non-ASCII untouched.

    Mirrors Calibre's own convention of substituting `_` for illegal characters,
    so `Dune: House Atreides` becomes `Dune_ House Atreides`.
    """
    swapped = "".join(replacement if _is_illegal(char) else char for char in text)
    # Collapse whitespace runs so substitution cannot leave odd gaps, and so the
    # result is stable regardless of stray spacing in the metadata.
    return " ".join(swapped.split())


def format_series_index(value: float) -> str:
    """Render a series index so a series sorts lexicographically into order.

    Zero-pads the integer part to two digits and drops a trailing `.0`, so
    `1.0` -> `01` and `2.5` -> `02.5`. Integer parts wider than two digits are
    left alone rather than truncated.
    """
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    integer_part, _, fraction = text.partition(".")
    try:
        padded = f"{int(integer_part):02d}"
    except ValueError:
        padded = integer_part
    return f"{padded}.{fraction}" if fraction else padded


def build_flat_name(
    book: BookNaming,
    *,
    max_length: int,
    replacement: str = "_",
    include_id: bool = False,
) -> str:
    """Render one book's flat filename.

    Only the title is ever truncated; the extension, series index, and the
    collision-disambiguating id suffix are always preserved in full.
    """
    author = sanitize_component(book.author_sort, replacement) or "Unknown"
    title = sanitize_component(book.title, replacement) or f"book {book.book_id}"
    extension = f".{book.extension.casefold().lstrip('.')}"
    suffix = f" ({book.book_id})" if include_id else ""

    if book.series:
        series = sanitize_component(book.series, replacement)
        index = format_series_index(book.series_index)
        prefix = f"{author} - {series} {index} - " if series else f"{author} - "
    else:
        prefix = f"{author} - "

    tail_cost = utf16_length(suffix) + utf16_length(extension)
    title_budget = max_length - utf16_length(prefix) - tail_cost

    if title_budget >= _MIN_TITLE_BUDGET:
        stem = prefix + truncate_utf16(title, title_budget)
    else:
        # Pathological case: the fixed parts alone blow the budget. Preserve the
        # suffix and extension and hard-truncate everything else.
        stem = truncate_utf16(prefix + title, max_length - tail_cost)

    stem = stem.rstrip(_TRAILING_JUNK)
    if not stem:
        stem = f"book {book.book_id}"
    return f"{stem}{suffix}{extension}"


def assign_flat_names(
    books: Iterable[BookNaming],
    *,
    max_length: int,
    replacement: str = "_",
) -> Mapping[int, str]:
    """Map book id -> flat name, disambiguating only the names that collide.

    Collision detection is case-insensitive because the destination filesystem
    is: two names differing only in case are distinct over WebDAV but would
    clobber each other once mirrored onto the Kobo's FAT32 storage.
    """
    books = list(books)
    plain = {
        book.book_id: build_flat_name(book, max_length=max_length, replacement=replacement)
        for book in books
    }
    taken = Counter(name.casefold() for name in plain.values())
    return {
        book.book_id: (
            build_flat_name(book, max_length=max_length, replacement=replacement, include_id=True)
            if taken[plain[book.book_id].casefold()] > 1
            else plain[book.book_id]
        )
        for book in books
    }
