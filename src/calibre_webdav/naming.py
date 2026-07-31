"""Rendering a book to a relative path from an operator-supplied template.

The template syntax is Calibre's own save-template shape:

    {field}                  the field's value
    {field:|prefix|suffix}   prefix + value + suffix, or nothing at all when the
                             field is empty

`/` in a template separates path components, so the depth of the served tree is
the operator's choice. Field values are sanitized before substitution, which is
what makes it impossible for a title like `AC/DC` to invent a path component.

Everything here is pure: no filesystem, no database.
"""

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

# Illegal in a rendered component whatever the target filesystem is: `/` would
# silently split one component into two, and control characters have no business
# in a filename anywhere. Always replaced, even when FAT32 handling is off.
ALWAYS_ILLEGAL_CHARS = frozenset("/")

# Illegal on FAT32 and Windows, perfectly legal on ext4. Replaced only when the
# target is FAT32.
FAT32_ILLEGAL_CHARS = frozenset(':*?"<>|\\')

# What every illegal character becomes. Also Calibre's own convention.
REPLACEMENT = "_"

# Characters FAT32 forbids at the very end of a name. A dot or space mid-name is
# fine; a trailing one makes the write fail on-device.
_TRAILING_JUNK = " ."

# `{field}` or `{field:|prefix|suffix}`. Prefix and suffix are literal text and
# may not themselves contain braces or the `|` that delimits them.
_PLACEHOLDER = re.compile(r"\{(\w+)(?::\|([^|{}]*)\|([^|{}]*))?\}")


class TemplateError(ValueError):
    """Raised when a path template cannot be rendered as written."""


@dataclass(frozen=True, slots=True)
class BookNaming:
    """The naming-relevant fields of one book, straight out of metadata.db."""

    book_id: int
    author_sort: str
    title: str
    extension: str
    title_sort: str | None = None
    series: str | None = None
    series_index: float = 1.0
    year: int | None = None


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


# The template vocabulary. Being the single source of truth for both validation
# and rendering, an unknown field can only be a typo in the template.
FIELDS: Mapping[str, Callable[[BookNaming], str]] = {
    "author_sort": lambda book: book.author_sort,
    "title": lambda book: book.title,
    "title_sort": lambda book: book.title_sort or book.title,
    "series": lambda book: book.series or "",
    # An index without a series is meaningless, and Calibre defaults it to 1.0
    # for every standalone book, so it renders empty unless there is a series.
    "series_index": lambda book: format_series_index(book.series_index) if book.series else "",
    "id": lambda book: str(book.book_id),
    "year": lambda book: str(book.year) if book.year else "",
    "ext": lambda book: book.extension.casefold().lstrip("."),
}


@dataclass(frozen=True, slots=True)
class Placeholder:
    """One `{field}` or `{field:|prefix|suffix}` in a parsed template."""

    field: str
    prefix: str = ""
    suffix: str = ""


# A parsed template: literal text interleaved with placeholders.
PathTemplate = tuple[str | Placeholder, ...]


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


def _illegal_chars(fat32: bool) -> frozenset[str]:
    """The characters replaced under a given FAT32 setting."""
    if not fat32:
        return ALWAYS_ILLEGAL_CHARS
    return ALWAYS_ILLEGAL_CHARS | FAT32_ILLEGAL_CHARS


def sanitize_component(text: str, fat32: bool = True) -> str:
    """Make one field value safe to drop into a path component.

    A `fat32` of False means the target is not FAT32: only the always-illegal
    set is substituted. Non-ASCII is left untouched either way.
    """
    illegal = _illegal_chars(fat32)
    swapped = "".join(
        REPLACEMENT if char in illegal or ord(char) < 0x20 or ord(char) == 0x7F else char
        for char in text
    )
    # Collapse whitespace runs so substitution cannot leave odd gaps, and so the
    # result is stable regardless of stray spacing in the metadata.
    collapsed = " ".join(swapped.split())
    # A value of bare dots would mean "this directory" or "the parent one".
    return REPLACEMENT * len(collapsed) if collapsed in {".", ".."} else collapsed


def parse_template(text: str, *, fat32: bool = True) -> PathTemplate:
    """Parse a template, rejecting anything that cannot render a legal path.

    `fat32` is consulted only to decide whether the template's own literal text
    has to be FAT32-legal: an operator who writes a `:` into a template aimed at
    a Kobo wants to hear about it at startup rather than discover unwritable
    files on the device later.
    """
    nodes: list[str | Placeholder] = []
    position = 0
    for match in _PLACEHOLDER.finditer(text):
        nodes.append(text[position : match.start()])
        field, prefix, suffix = match.group(1), match.group(2) or "", match.group(3) or ""
        if field not in FIELDS:
            raise TemplateError(
                f"unknown template field {{{field}}}. "
                f"The known fields are {', '.join(sorted(FIELDS))}."
            )
        nodes.append(Placeholder(field, prefix, suffix))
        position = match.end()
    nodes.append(text[position:])

    template = tuple(node for node in nodes if node != "")
    _validate(template, fat32=fat32)
    return template


def _validate(template: PathTemplate, *, fat32: bool) -> None:
    literals = [node for node in template if isinstance(node, str)]
    literals += [node.prefix + node.suffix for node in template if isinstance(node, Placeholder)]
    # `/` is the separator and so is legal in the template's own text, whatever
    # it would mean inside a field value.
    illegal = _illegal_chars(fat32) - ALWAYS_ILLEGAL_CHARS

    for literal in literals:
        stray = next((char for char in literal if char in "{}"), None)
        if stray is not None:
            raise TemplateError(f"stray {stray!r} in the template. A placeholder is malformed.")
        offender = next((char for char in literal if char in illegal), None)
        if offender is not None:
            raise TemplateError(
                f"the template contains {offender!r}, which is illegal in a FAT32 filename. "
                "If the target is not FAT32, set CW_FAT32=false."
            )

    if not any(isinstance(node, Placeholder) and node.field == "ext" for node in template):
        raise TemplateError("the template must contain {ext} or the files have no extension")

    # A literal `.` or `..` between two separators would be real path traversal
    # rather than a naming quirk, so it is refused outright.
    skeleton = "".join(
        node if isinstance(node, str) else f"{node.prefix}\x00{node.suffix}" for node in template
    )
    for segment in skeleton.split("/"):
        if segment and set(segment) <= set("."):
            raise TemplateError(f"the template has a {segment!r} path segment")


def render_path(
    book: BookNaming,
    template: PathTemplate,
    *,
    max_length: int,
    fat32: bool = True,
    include_id: bool = False,
) -> tuple[str, ...]:
    """Render one book to its relative path, as a tuple of path components.

    Components that render empty are dropped, which is how an optional series
    directory collapses. Each surviving component is capped independently at
    `max_length`; on the last one the extension and the collision-disambiguating
    id suffix are preserved and only the stem is truncated.
    """
    rendered = "".join(_render_node(node, book, fat32) for node in template)
    components = [component for component in rendered.split("/") if component] or [""]

    extension = f".{FIELDS['ext'](book)}"
    tail = extension if components[-1].endswith(extension) else ""
    if include_id:
        components[-1] = f"{components[-1].removesuffix(tail)} ({book.book_id}){tail}"
        tail = f" ({book.book_id}){tail}"

    fitted = [_fit(component, max_length, fat32=fat32) for component in components[:-1]]
    fitted.append(_fit(components[-1], max_length, tail=tail, fat32=fat32))

    fallback = f"book {book.book_id}{tail}"
    return tuple(component or fallback for component in fitted)


def _render_node(node: str | Placeholder, book: BookNaming, fat32: bool) -> str:
    if isinstance(node, str):
        return node
    value = sanitize_component(FIELDS[node.field](book), fat32)
    return f"{node.prefix}{value}{node.suffix}" if value else ""


def _fit(component: str, limit: int, *, fat32: bool, tail: str = "") -> str:
    """Cap one component, keeping `tail` intact and truncating only the stem."""
    stem = component.removesuffix(tail)
    if utf16_length(component) > limit:
        stem = truncate_utf16(stem, limit - utf16_length(tail))
    if fat32:
        stem = stem.rstrip(_TRAILING_JUNK)
    return f"{stem}{tail}" if stem else ""


def assign_paths(
    books: Iterable[BookNaming],
    template: PathTemplate,
    *,
    max_length: int,
    fat32: bool = True,
) -> Mapping[int, tuple[str, ...]]:
    """Map book id -> path components, disambiguating only the paths that collide.

    Collision detection is case-insensitive because the destination filesystem
    may be: two paths differing only in case are distinct over WebDAV but would
    clobber each other once mirrored onto a Kobo's FAT32 storage.
    """
    books = list(books)

    def render(book: BookNaming, *, include_id: bool) -> tuple[str, ...]:
        return render_path(
            book, template, max_length=max_length, fat32=fat32, include_id=include_id
        )

    plain = {book.book_id: render(book, include_id=False) for book in books}
    taken = Counter(_folded(path) for path in plain.values())
    return {
        book.book_id: (
            render(book, include_id=True)
            if taken[_folded(plain[book.book_id])] > 1
            else plain[book.book_id]
        )
        for book in books
    }


def _folded(path: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(component.casefold() for component in path)
