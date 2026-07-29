"""A small synthetic Calibre library, so tests never touch the real one."""

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from fcws.config import Config

# A faithful subset of Calibre's schema: exactly the tables and columns the
# index query touches.
_SCHEMA = """
CREATE TABLE books (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'Unknown',
    sort TEXT,
    series_index REAL NOT NULL DEFAULT 1.0,
    author_sort TEXT,
    path TEXT NOT NULL DEFAULT '',
    has_cover BOOL DEFAULT 0,
    uuid TEXT,
    timestamp TIMESTAMP,
    last_modified TIMESTAMP
);
CREATE TABLE data (
    id INTEGER PRIMARY KEY,
    book INTEGER NOT NULL,
    format TEXT NOT NULL COLLATE NOCASE,
    uncompressed_size INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    UNIQUE(book, format)
);
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT NOT NULL, sort TEXT);
CREATE TABLE books_series_link (
    id INTEGER PRIMARY KEY,
    book INTEGER NOT NULL,
    series INTEGER NOT NULL,
    UNIQUE(book)
);
"""


class LibraryBuilder:
    """Builds a Calibre-shaped library on disk: metadata.db plus real files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = root / "metadata.db"
        self._connection = sqlite3.connect(self.database_path)
        self._connection.executescript(_SCHEMA)
        self._next_id = 1
        self._series: dict[str, int] = {}

    def add_book(
        self,
        title: str,
        author_sort: str,
        *,
        book_id: int | None = None,
        path: str | None = None,
        formats: tuple[str, ...] = ("EPUB",),
        series: str | None = None,
        series_index: float = 1.0,
        create_files: bool | tuple[str, ...] = True,
        content: bytes = b"book contents",
    ) -> int:
        """Add one book. `create_files` may be a subset of `formats` to write."""
        book_id = book_id if book_id is not None else self._next_id
        self._next_id = max(self._next_id, book_id) + 1
        path = path if path is not None else f"{author_sort}/{title} ({book_id})"

        self._connection.execute(
            "INSERT INTO books (id, title, author_sort, path, series_index) VALUES (?, ?, ?, ?, ?)",
            (book_id, title, author_sort, path, series_index),
        )
        if series is not None:
            series_id = self._series.get(series)
            if series_id is None:
                series_id = len(self._series) + 1
                self._series[series] = series_id
                self._connection.execute(
                    "INSERT INTO series (id, name) VALUES (?, ?)", (series_id, series)
                )
            self._connection.execute(
                "INSERT INTO books_series_link (book, series) VALUES (?, ?)",
                (book_id, series_id),
            )

        written = formats if create_files is True else tuple(create_files or ())
        directory = self.root / path
        directory.mkdir(parents=True, exist_ok=True)
        for fmt in formats:
            data_name = f"{title} - {author_sort}"[:80]
            self._connection.execute(
                "INSERT INTO data (book, format, name) VALUES (?, ?, ?)",
                (book_id, fmt.upper(), data_name),
            )
            if fmt in written:
                (directory / f"{data_name}.{fmt.lower()}").write_bytes(content)

        self._connection.commit()
        return book_id

    def add_book_without_formats(self, title: str, author_sort: str) -> int:
        """A `books` row with zero `data` rows, which must be skipped silently."""
        book_id = self._next_id
        self._next_id += 1
        self._connection.execute(
            "INSERT INTO books (id, title, author_sort, path) VALUES (?, ?, ?, ?)",
            (book_id, title, author_sort, f"{author_sort}/{title} ({book_id})"),
        )
        self._connection.commit()
        return book_id

    def config(self, **overrides) -> Config:
        base = Config.from_env(
            {
                "FCWS_LIBRARY_ROOT": str(self.root),
                "FCWS_USERNAME": "user",
                "FCWS_PASSWORD": "pass",
                # Zero debounce keeps freshness tests deterministic.
                "FCWS_INDEX_DEBOUNCE_SECONDS": "0",
            }
        )
        return replace(base, **overrides)

    def close(self) -> None:
        self._connection.close()


@pytest.fixture
def library(tmp_path: Path) -> Iterator[LibraryBuilder]:
    builder = LibraryBuilder(tmp_path / "library")
    yield builder
    builder.close()
