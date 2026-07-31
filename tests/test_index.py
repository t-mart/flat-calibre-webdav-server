"""Index building: discovery from metadata.db only, plus drift handling."""

import logging
import os
from types import MappingProxyType
from typing import Any

import pytest

from calibre_webdav.index import IndexCache, build_index, database_signature

NESTED = "{author_sort}/{series:||/}{series_index:|| - }{title}.{ext}"


class TestDiscovery:
    def test_books_are_discovered_from_the_database(self, library):
        library.add_book("Dune", "Herbert, Frank")
        library.add_book("2666", "Bolaño, Roberto")
        index = build_index(library.config())
        assert set(index.paths()) == {
            "Herbert, Frank - Dune.epub",
            "Bolaño, Roberto - 2666.epub",
        }

    def test_entries_point_at_real_files(self, library):
        library.add_book("Dune", "Herbert, Frank", content=b"dune bytes")
        index = build_index(library.config())
        entry = index.get("Herbert, Frank - Dune.epub")
        assert entry is not None
        assert entry.real_path.read_bytes() == b"dune bytes"

    def test_stray_file_not_in_the_database_is_ignored(self, library):
        library.add_book("Dune", "Herbert, Frank")
        (library.root / "stray.epub").write_bytes(b"not in the database")
        index = build_index(library.config())
        assert index.paths() == ["Herbert, Frank - Dune.epub"]

    def test_live_index_cannot_be_mutated(self, library):
        # A rebuild swaps in a new snapshot; nothing may edit the live one.
        library.add_book("Dune", "Herbert, Frank")
        index = build_index(library.config())
        assert isinstance(index.entries, MappingProxyType)
        mutable: Any = index.entries
        with pytest.raises(TypeError):
            mutable["injected"] = None


class TestTemplate:
    def test_the_default_template_serves_everything_from_the_root(self, library):
        library.add_book("Dune", "Herbert, Frank")
        index = build_index(library.config())
        assert index.paths() == ["Herbert, Frank - Dune.epub"]
        assert index.children("") == ("Herbert, Frank - Dune.epub",)

    def test_a_nested_template_builds_directories(self, library):
        library.add_book("Hatchet", "Paulsen, Gary", series="Brian's Saga", series_index=1.0)
        library.add_book("Interlude", "Paulsen, Gary", series="Brian's Saga", series_index=2.5)
        library.add_book("Standalone", "Paulsen, Gary")
        index = build_index(library.config(template=NESTED))
        assert index.paths() == [
            "Paulsen, Gary/Brian's Saga/01 - Hatchet.epub",
            "Paulsen, Gary/Brian's Saga/02.5 - Interlude.epub",
            "Paulsen, Gary/Standalone.epub",
        ]

    def test_year_comes_from_pubdate(self, library):
        library.add_book("Dune", "Herbert, Frank", year=1965)
        index = build_index(library.config(template="{year}/{title}.{ext}"))
        assert index.paths() == ["1965/Dune.epub"]

    def test_calibres_undefined_pubdate_is_not_a_year(self, library):
        library.add_book("Dune", "Herbert, Frank")
        index = build_index(library.config(template="{year:||/}{title}.{ext}"))
        assert index.paths() == ["Dune.epub"]

    def test_title_sort_comes_from_the_sort_column(self, library):
        library.add_book("The Hobbit", "Tolkien, J.R.R.", title_sort="Hobbit, The")
        index = build_index(library.config(template="{title_sort}.{ext}"))
        assert index.paths() == ["Hobbit, The.epub"]


class TestTree:
    def test_directories_are_derived_from_the_paths(self, library):
        library.add_book("Hatchet", "Paulsen, Gary", series="Brian's Saga")
        library.add_book("Dune", "Herbert, Frank")
        index = build_index(library.config(template=NESTED))
        assert index.is_collection("")
        assert index.is_collection("Paulsen, Gary")
        assert index.is_collection("Paulsen, Gary/Brian's Saga")
        assert not index.is_collection("Herbert, Frank/Dune.epub")

    def test_children_are_the_immediate_members_only(self, library):
        library.add_book("Hatchet", "Paulsen, Gary", series="Brian's Saga")
        library.add_book("Standalone", "Paulsen, Gary")
        index = build_index(library.config(template=NESTED))
        assert index.children("") == ("Paulsen, Gary",)
        assert index.children("Paulsen, Gary") == (
            "Paulsen, Gary/Brian's Saga",
            "Paulsen, Gary/Standalone.epub",
        )

    def test_an_empty_library_still_has_a_root(self, library):
        index = build_index(library.config())
        assert index.is_collection("")
        assert index.children("") == ()

    def test_a_book_that_is_also_a_directory_is_dropped(self, library, caplog):
        # Takes a series named after a whole filename to provoke, since the
        # extension normally keeps books and directories apart. The directory
        # wins, because dropping it would take its contents with it.
        library.add_book("Sequel", "Herbert, Frank", series="Dune.epub", series_index=1.0)
        library.add_book("Dune", "Herbert, Frank", path="elsewhere")
        with caplog.at_level(logging.WARNING):
            index = build_index(library.config(template=NESTED))
        assert index.paths() == ["Herbert, Frank/Dune.epub/01 - Sequel.epub"]
        assert index.skipped == 1
        assert "is also a directory" in caplog.text


class TestDrift:
    def test_book_with_zero_formats_is_skipped_silently(self, library, caplog):
        library.add_book("Dune", "Herbert, Frank")
        library.add_book_without_formats("Phantom", "Nobody, A")
        with caplog.at_level(logging.WARNING):
            index = build_index(library.config())
        assert index.paths() == ["Herbert, Frank - Dune.epub"]
        assert not caplog.records

    def test_missing_file_is_skipped_with_a_warning(self, library, caplog):
        library.add_book("Dune", "Herbert, Frank")
        library.add_book("Ghost", "Missing, M", create_files=())
        with caplog.at_level(logging.WARNING):
            index = build_index(library.config())
        assert index.paths() == ["Herbert, Frank - Dune.epub"]
        assert index.skipped == 1
        assert "missing on disk" in caplog.text
        assert "Ghost" in caplog.text

    def test_drift_does_not_abort_the_build(self, library):
        library.add_book("Ghost", "Missing, M", create_files=())
        library.add_book("Dune", "Herbert, Frank")
        index = build_index(library.config())
        assert len(index.entries) == 1

    def test_path_escaping_the_library_root_is_refused(self, library, caplog):
        library.add_book("Escape", "Evil, E", path="../../etc")
        with caplog.at_level(logging.WARNING):
            index = build_index(library.config())
        assert index.entries == {}
        assert "unsafe path" in caplog.text


class TestFormatPreference:
    def test_multi_format_book_appears_once_as_the_preferred_format(self, library):
        library.add_book("Dune", "Herbert, Frank", formats=("EPUB", "PDF"))
        index = build_index(library.config())
        assert index.paths() == ["Herbert, Frank - Dune.epub"]

    def test_preference_order_is_configurable(self, library):
        library.add_book("Dune", "Herbert, Frank", formats=("EPUB", "PDF"))
        index = build_index(library.config(format_preference=("pdf", "epub")))
        assert index.paths() == ["Herbert, Frank - Dune.pdf"]

    def test_falls_through_when_the_preferred_file_is_missing(self, library, caplog):
        # The EPUB is listed in the database but only the PDF is on disk.
        library.add_book("Dune", "Herbert, Frank", formats=("EPUB", "PDF"), create_files=("PDF",))
        with caplog.at_level(logging.WARNING):
            index = build_index(library.config())
        assert index.paths() == ["Herbert, Frank - Dune.pdf"]
        assert "missing on disk" in caplog.text

    def test_book_with_no_preferred_format_is_skipped_with_a_warning(self, library, caplog):
        library.add_book("Old", "Legacy, L", formats=("MOBI",))
        with caplog.at_level(logging.WARNING):
            index = build_index(library.config())
        assert index.entries == {}
        assert index.skipped == 1
        assert "no format in preference order" in caplog.text


class TestCollisions:
    def test_colliding_books_get_id_suffixes(self, library):
        first = library.add_book("Dune", "Herbert, Frank", path="a")
        second = library.add_book("Dune", "Herbert, Frank", path="b")
        index = build_index(library.config())
        assert index.paths() == [
            f"Herbert, Frank - Dune ({first}).epub",
            f"Herbert, Frank - Dune ({second}).epub",
        ]

    def test_non_colliding_books_stay_clean(self, library):
        library.add_book("Dune", "Herbert, Frank", path="a")
        library.add_book("Dune Messiah", "Herbert, Frank", path="b")
        index = build_index(library.config())
        assert index.paths() == [
            "Herbert, Frank - Dune Messiah.epub",
            "Herbert, Frank - Dune.epub",
        ]


class TestFreshness:
    def test_signature_changes_when_the_database_is_touched(self, library):
        before = database_signature(library.database_path)
        os.utime(library.database_path, (1, 1))
        assert database_signature(library.database_path) != before

    def test_signature_of_a_missing_database_is_none(self, tmp_path):
        assert database_signature(tmp_path / "nope.db") is None

    def test_cache_picks_up_a_newly_ingested_book(self, library):
        library.add_book("Dune", "Herbert, Frank")
        cache = IndexCache(library.config())
        assert len(cache.current().entries) == 1

        library.add_book("2666", "Bolaño, Roberto")
        os.utime(library.database_path, None)
        assert len(cache.current().entries) == 2

    def test_cache_reuses_the_index_when_nothing_changed(self, library):
        library.add_book("Dune", "Herbert, Frank")
        cache = IndexCache(library.config())
        first = cache.current()
        assert cache.current() is first

    def test_debounce_suppresses_restat(self, library):
        library.add_book("Dune", "Herbert, Frank")
        cache = IndexCache(library.config(index_debounce_seconds=3600))
        first = cache.current()
        library.add_book("2666", "Bolaño, Roberto")
        os.utime(library.database_path, None)
        assert cache.current() is first

    def test_failed_rebuild_keeps_the_previous_index(self, library, caplog):
        library.add_book("Dune", "Herbert, Frank")
        cache = IndexCache(library.config())
        good = cache.current()
        assert len(good.entries) == 1

        library.close()
        library.database_path.unlink()
        with caplog.at_level(logging.ERROR):
            after = cache.refresh()
        assert after.paths() == good.paths()
        assert "rebuild failed" in caplog.text
