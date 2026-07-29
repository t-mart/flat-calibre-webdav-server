"""Building and caching the flat book index.

Discovery comes entirely from `metadata.db`. The library tree is never walked;
the filesystem is consulted only to confirm that a file a `data` row points at
actually exists.
"""

import logging
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .config import Config
from .naming import BookNaming, assign_flat_names

log = logging.getLogger(__name__)

# One query for the whole library. `data` is joined inner, so a book with zero
# formats simply never appears: that is the "skip silently" rule. Series is a
# LEFT join because most books have none.
#
# `books.author_sort` is used rather than joining `authors`: it is Calibre's own
# canonical sort string and already renders multi-author books as
# "Herbert, Brian & Anderson, Kevin J.", which a join would fan out into
# duplicate rows instead.
_QUERY = """
SELECT
    b.id,
    b.author_sort,
    b.title,
    b.series_index,
    b.path,
    s.name AS series_name,
    d.name AS data_name,
    d.format
FROM books b
JOIN data d ON d.book = b.id
LEFT JOIN books_series_link bsl ON bsl.book = b.id
LEFT JOIN series s ON s.id = bsl.series
"""


@dataclass(frozen=True, slots=True)
class BookEntry:
    """One resolvable book: a flat name pointing at a real file on disk."""

    book_id: int
    flat_name: str
    real_path: Path


@dataclass(frozen=True, slots=True)
class LibraryIndex:
    """An immutable snapshot of the library. Never mutated after construction."""

    entries: Mapping[str, BookEntry] = field(default_factory=dict)
    skipped: int = 0
    signature: tuple[int, int] | None = None
    built_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", MappingProxyType(dict(self.entries)))

    def get(self, flat_name: str) -> BookEntry | None:
        return self.entries.get(flat_name)

    def names(self) -> list[str]:
        return sorted(self.entries)


def database_signature(database_path: Path) -> tuple[int, int] | None:
    """Cheap freshness key for metadata.db.

    Calibre and CWA touch the database on every change, so stat'ing this one
    file is a complete freshness check for the whole library. Size is included
    alongside mtime to catch two writes landing inside one mtime granule.
    """
    try:
        stat = database_path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _connect(database_path: Path, timeout: float) -> sqlite3.Connection:
    """Open metadata.db strictly read-only.

    The library is not in WAL mode, so a `mode=ro` URI connection is safe. If
    `metadata.db-wal` ever appears (a Calibre or CWA upgrade re-enabling WAL),
    this call will start failing: WAL readers need write access to the `-shm`
    file, which `mode=ro` denies. The fix at that point is to `shutil.copy2` the
    database plus any `-wal`/`-shm` into a scratch directory and read the copy.
    Never write here, never VACUUM, never take a write lock.
    """
    return sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True, timeout=timeout)


def _fetch_rows(config: Config) -> Sequence[sqlite3.Row]:
    """Run the index query, retrying briefly if a writer holds the database.

    In rollback-journal mode a writer holding an EXCLUSIVE lock makes readers
    fail with SQLITE_BUSY, so CWA ingesting mid-rebuild is expected, not
    exceptional.
    """
    last_error: sqlite3.Error | None = None
    for attempt in range(1, config.db_retry_attempts + 1):
        try:
            connection = _connect(config.database_path, config.db_timeout_seconds)
            try:
                connection.row_factory = sqlite3.Row
                return connection.execute(_QUERY).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as error:
            last_error = error
            if attempt < config.db_retry_attempts:
                delay = 0.25 * attempt
                log.warning(
                    "metadata.db read failed (attempt %d/%d): %s; retrying in %.2fs",
                    attempt,
                    config.db_retry_attempts,
                    error,
                    delay,
                )
                time.sleep(delay)
    raise last_error if last_error else sqlite3.Error("index query failed")


def _safe_relative_path(raw: str) -> PurePosixPath | None:
    """Reject anything that would escape the library root.

    Calibre stores a forward-slash relative directory here, but these values end
    up as served file paths, so they are validated rather than trusted.
    """
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        return None
    return candidate


def _resolve_file(
    book_id: int,
    book_path: str,
    formats: Mapping[str, str],
    config: Config,
) -> tuple[str, Path] | None:
    """Pick the best available format whose file is actually on disk.

    Walks the preference order and returns the first candidate that exists,
    warning about any listed-but-missing file it passes over on the way.
    """
    relative = _safe_relative_path(book_path)
    if relative is None:
        log.warning("book %s: refusing unsafe path %r from metadata.db", book_id, book_path)
        return None

    usable = [fmt for fmt in config.format_preference if fmt in formats]
    if not usable:
        log.warning(
            "book %s: no format in preference order (%s); has %s",
            book_id,
            ", ".join(config.format_preference),
            ", ".join(sorted(formats)) or "none",
        )
        return None

    for fmt in usable:
        data_name = formats[fmt]
        if "/" in data_name or "\\" in data_name or data_name in {"", ".", ".."}:
            log.warning("book %s: refusing unsafe filename %r from metadata.db", book_id, data_name)
            continue
        candidate = config.library_root / relative / f"{data_name}.{fmt}"
        if candidate.is_file():
            return fmt, candidate
        log.warning(
            "book %s: %s listed in metadata.db but missing on disk: %s",
            book_id,
            fmt.upper(),
            candidate,
        )
    return None


def build_index(config: Config) -> LibraryIndex:
    """Query the database and resolve every book to a flat name and real file."""
    started = time.monotonic()
    signature = database_signature(config.database_path)
    rows = _fetch_rows(config)

    # Group the per-format rows back into one record per book.
    books: dict[int, dict] = {}
    for row in rows:
        record = books.setdefault(
            row["id"],
            {
                "author_sort": row["author_sort"] or "Unknown",
                "title": row["title"] or "Unknown",
                "series": row["series_name"],
                "series_index": row["series_index"],
                "path": row["path"] or "",
                "formats": {},
            },
        )
        fmt = (row["format"] or "").casefold()
        if fmt:
            record["formats"][fmt] = row["data_name"]

    resolved: list[tuple[BookNaming, Path]] = []
    skipped = 0
    for book_id, record in books.items():
        chosen = _resolve_file(book_id, record["path"], record["formats"], config)
        if chosen is None:
            skipped += 1
            continue
        extension, real_path = chosen
        resolved.append(
            (
                BookNaming(
                    book_id=book_id,
                    author_sort=record["author_sort"],
                    title=record["title"],
                    extension=extension,
                    series=record["series"],
                    series_index=_as_float(record["series_index"]),
                ),
                real_path,
            )
        )

    names = assign_flat_names(
        (naming for naming, _ in resolved),
        max_length=config.max_filename_length,
        replacement=config.sanitize_replacement,
    )
    entries = {
        names[naming.book_id]: BookEntry(
            book_id=naming.book_id,
            flat_name=names[naming.book_id],
            real_path=real_path,
        )
        for naming, real_path in resolved
    }

    log.info(
        "index built: %d books, %d skipped, in %.0f ms",
        len(entries),
        skipped,
        (time.monotonic() - started) * 1000,
    )
    return LibraryIndex(
        entries=entries,
        skipped=skipped,
        signature=signature,
        built_at=time.monotonic(),
    )


def _as_float(value) -> float:
    try:
        return float(value)
    except TypeError, ValueError:
        return 1.0


class IndexCache:
    """Holds the live index and rebuilds it when metadata.db moves.

    Rebuilds are atomic by construction: a fresh `LibraryIndex` is built off to
    the side and the reference is swapped, so a PROPFIND racing a rebuild sees
    either the old snapshot or the new one and never a half-built listing. A
    failed rebuild logs and leaves the previous index in place.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._index = LibraryIndex()
        self._last_checked = 0.0

    def current(self) -> LibraryIndex:
        """Return the live index, refreshing first if the database has changed."""
        now = time.monotonic()
        if now - self._last_checked < self._config.index_debounce_seconds:
            return self._index

        with self._lock:
            # Another thread may have refreshed while we waited for the lock.
            if time.monotonic() - self._last_checked < self._config.index_debounce_seconds:
                return self._index
            self._last_checked = time.monotonic()
            signature = database_signature(self._config.database_path)
            if signature is not None and signature == self._index.signature:
                return self._index
            self._rebuild_locked()
        return self._index

    def refresh(self) -> LibraryIndex:
        """Force a rebuild regardless of debounce or signature."""
        with self._lock:
            self._last_checked = time.monotonic()
            self._rebuild_locked()
        return self._index

    def _rebuild_locked(self) -> None:
        try:
            self._index = build_index(self._config)
        except Exception:
            log.exception(
                "index rebuild failed; keeping previous index of %d book(s)",
                len(self._index.entries),
            )
