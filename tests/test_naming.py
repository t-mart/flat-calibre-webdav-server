"""Naming is a contract shared with the KOReader client, so it is pinned tightly."""

import pytest

from calibre_webdav.naming import (
    BookNaming,
    assign_flat_names,
    build_flat_name,
    format_series_index,
    sanitize_component,
    truncate_utf16,
    utf16_length,
)

MAX = 200


def naming(
    *,
    book_id: int = 1,
    author_sort: str = "Herbert, Frank",
    title: str = "Dune",
    extension: str = "epub",
    series: str | None = None,
    series_index: float = 1.0,
) -> BookNaming:
    return BookNaming(
        book_id=book_id,
        author_sort=author_sort,
        title=title,
        extension=extension,
        series=series,
        series_index=series_index,
    )


def name_of(**overrides) -> str:
    return build_flat_name(naming(**overrides), max_length=MAX)


class TestFormatSeriesIndex:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1.0, "01"),
            (2.0, "02"),
            (2.5, "02.5"),
            (0.5, "00.5"),
            (10.0, "10"),
            (100.0, "100"),
            (1.25, "01.25"),
        ],
    )
    def test_renders(self, value, expected):
        assert format_series_index(value) == expected

    def test_series_sorts_into_reading_order(self):
        indices = [3.0, 1.0, 10.0, 2.5, 2.0]
        rendered = sorted(format_series_index(value) for value in indices)
        assert rendered == ["01", "02", "02.5", "03", "10"]


class TestSanitize:
    @pytest.mark.parametrize("char", list(':*?"<>|\\/'))
    def test_replaces_every_fat32_illegal_character(self, char):
        assert char not in sanitize_component(f"a{char}b")

    def test_colon_becomes_underscore(self):
        assert sanitize_component("Dune: House Atreides") == "Dune_ House Atreides"

    def test_slash_becomes_underscore(self):
        # Also protects the WebDAV path, which `/` would otherwise split.
        assert sanitize_component("A LitRPG/Gamelit Adventure") == "A LitRPG_Gamelit Adventure"

    def test_non_ascii_survives_intact(self):
        assert sanitize_component("Bolaño, Roberto") == "Bolaño, Roberto"

    def test_control_characters_are_replaced(self):
        assert sanitize_component("a\x00b\x1fc\x7fd") == "a_b_c_d"

    def test_whitespace_is_collapsed_and_trimmed(self):
        assert sanitize_component("  a   b  ") == "a b"

    def test_respects_custom_replacement(self):
        assert sanitize_component("Dune: House", " - ") == "Dune - House"


class TestUtf16:
    def test_length_counts_astral_characters_as_two(self):
        assert utf16_length("ab") == 2
        assert utf16_length("ñ") == 1
        assert utf16_length("\U0001f600") == 2

    def test_truncate_never_splits_a_character(self):
        # A 2-unit character cannot fit in 1 unit, so it is dropped whole.
        assert truncate_utf16("\U0001f600b", 1) == ""
        assert truncate_utf16("\U0001f600b", 2) == "\U0001f600"
        assert truncate_utf16("\U0001f600b", 3) == "\U0001f600b"


class TestBuildFlatName:
    def test_without_series(self):
        assert name_of() == "Herbert, Frank - Dune.epub"

    def test_with_series(self):
        assert name_of(title="Dune Messiah", series="Dune", series_index=2.0) == (
            "Herbert, Frank - Dune 02 - Dune Messiah.epub"
        )

    def test_half_index_in_series(self):
        assert name_of(title="Interlude", series="Saga", series_index=2.5) == (
            "Herbert, Frank - Saga 02.5 - Interlude.epub"
        )

    def test_colon_in_title_is_sanitized(self):
        assert name_of(title="Dune: House Atreides") == (
            "Herbert, Frank - Dune_ House Atreides.epub"
        )

    def test_author_ending_in_dot_keeps_it_mid_name(self):
        assert name_of(author_sort="Salinger, J. D.", title="Nine Stories") == (
            "Salinger, J. D. - Nine Stories.epub"
        )

    def test_name_never_ends_in_dot_or_space(self):
        # A title that sanitizes to a trailing dot must not produce "....epub".
        result = name_of(title="Trailing dot.")
        assert not result.removesuffix(".epub").endswith((".", " "))

    def test_id_suffix_is_appended_before_the_extension(self):
        assert build_flat_name(naming(book_id=42), max_length=MAX, include_id=True) == (
            "Herbert, Frank - Dune (42).epub"
        )

    def test_long_title_is_truncated_to_the_cap(self):
        result = name_of(title="T" + "o" * 400)
        assert utf16_length(result) == MAX
        assert result.endswith(".epub")

    def test_truncation_preserves_extension_index_and_id(self):
        result = build_flat_name(
            naming(book_id=7, title="T" + "o" * 400, series="Saga", series_index=2.5),
            max_length=MAX,
            include_id=True,
        )
        assert utf16_length(result) == MAX
        assert result.startswith("Herbert, Frank - Saga 02.5 - ")
        assert result.endswith(" (7).epub")

    def test_extremely_small_budget_still_yields_a_legal_name(self):
        result = build_flat_name(
            naming(book_id=7, title="Some Title"), max_length=16, include_id=True
        )
        assert result.endswith(" (7).epub")
        assert utf16_length(result) <= 16

    def test_blank_author_falls_back_to_unknown(self):
        assert name_of(author_sort="   ") == "Unknown - Dune.epub"

    def test_blank_title_falls_back_to_the_book_id(self):
        assert build_flat_name(naming(book_id=5, title="  "), max_length=MAX) == (
            "Herbert, Frank - book 5.epub"
        )

    def test_title_of_only_illegal_characters_is_still_legal(self):
        # Sanitizes to "___" rather than to nothing, so no fallback is needed.
        assert name_of(title=":::") == "Herbert, Frank - ___.epub"


class TestAssignFlatNames:
    def test_non_colliding_names_stay_clean(self):
        books = [
            naming(book_id=1, title="Dune"),
            naming(book_id=2, title="Dune Messiah"),
        ]
        assigned = assign_flat_names(books, max_length=MAX)
        assert assigned[1] == "Herbert, Frank - Dune.epub"
        assert assigned[2] == "Herbert, Frank - Dune Messiah.epub"

    def test_only_colliding_names_get_an_id_suffix(self):
        books = [
            naming(book_id=1, title="Dune"),
            naming(book_id=2, title="Dune"),
            naming(book_id=3, title="Unique"),
        ]
        assigned = assign_flat_names(books, max_length=MAX)
        assert assigned[1] == "Herbert, Frank - Dune (1).epub"
        assert assigned[2] == "Herbert, Frank - Dune (2).epub"
        assert assigned[3] == "Herbert, Frank - Unique.epub"

    def test_collisions_are_detected_case_insensitively(self):
        # Distinct over WebDAV, but the same file on the Kobo's FAT32 storage.
        books = [naming(book_id=1, title="Dune"), naming(book_id=2, title="DUNE")]
        assigned = assign_flat_names(books, max_length=MAX)
        assert assigned[1].endswith("(1).epub")
        assert assigned[2].endswith("(2).epub")

    def test_titles_colliding_only_after_truncation_are_disambiguated(self):
        long_title = "Same " + "x" * 400
        books = [
            naming(book_id=1, title=long_title),
            naming(book_id=2, title=long_title),
        ]
        assigned = assign_flat_names(books, max_length=MAX)
        assert assigned[1] != assigned[2]
        assert len(set(assigned.values())) == 2

    def test_assigned_names_are_unique(self):
        books = [naming(book_id=index, title="Dune") for index in range(1, 6)]
        assigned = assign_flat_names(books, max_length=MAX)
        assert len(set(assigned.values())) == 5
