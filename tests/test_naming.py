"""Template parsing and path rendering, the shape of everything the client sees."""

import pytest

from calibre_webdav.config import DEFAULT_PATH_TEMPLATE
from calibre_webdav.naming import (
    BookNaming,
    TemplateError,
    assign_paths,
    format_series_index,
    parse_template,
    render_path,
    sanitize_component,
    truncate_utf16,
    utf16_length,
)

MAX = 200

FLAT = "{author_sort} - {series:|| }{series_index:|| - }{title}.{ext}"


def naming(
    *,
    book_id: int = 1,
    author_sort: str = "Herbert, Frank",
    title: str = "Dune",
    extension: str = "epub",
    title_sort: str | None = None,
    series: str | None = None,
    series_index: float = 1.0,
    year: int | None = None,
) -> BookNaming:
    return BookNaming(
        book_id=book_id,
        author_sort=author_sort,
        title=title,
        extension=extension,
        title_sort=title_sort,
        series=series,
        series_index=series_index,
        year=year,
    )


def path_of(template: str = DEFAULT_PATH_TEMPLATE, *, fat32=True, **overrides) -> str:
    components = render_path(
        naming(**overrides),
        parse_template(template, fat32=fat32),
        max_length=MAX,
        fat32=fat32,
    )
    return "/".join(components)


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
        # Also protects the path, which `/` would otherwise split in two.
        assert sanitize_component("A LitRPG/Gamelit Adventure") == "A LitRPG_Gamelit Adventure"

    def test_non_ascii_survives_intact(self):
        assert sanitize_component("Bolaño, Roberto") == "Bolaño, Roberto"

    def test_control_characters_are_replaced(self):
        assert sanitize_component("a\x00b\x1fc\x7fd") == "a_b_c_d"

    def test_whitespace_is_collapsed_and_trimmed(self):
        assert sanitize_component("  a   b  ") == "a b"

    def test_a_bare_dot_value_cannot_name_a_directory(self):
        assert sanitize_component("..") == "__"


class TestSanitizeWithoutFat32:
    def test_fat32_only_characters_survive(self):
        assert sanitize_component("Dune: House Atreides", False) == "Dune: House Atreides"

    def test_separator_is_still_replaced(self):
        # Not a FAT32 nicety: `/` would invent a path component.
        assert sanitize_component("AC/DC", False) == "AC_DC"

    def test_control_characters_are_still_replaced(self):
        assert sanitize_component("a\x00b", False) == "a_b"


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


class TestParseTemplate:
    def test_unknown_field_is_refused(self):
        with pytest.raises(TemplateError, match="unknown template field"):
            parse_template("{narrator}.{ext}")

    def test_error_lists_the_known_fields(self):
        with pytest.raises(TemplateError, match="author_sort"):
            parse_template("{narrator}.{ext}")

    def test_missing_extension_is_refused(self):
        with pytest.raises(TemplateError, match="must contain"):
            parse_template("{author_sort}/{title}")

    def test_unclosed_placeholder_is_refused(self):
        with pytest.raises(TemplateError, match="stray"):
            parse_template("{author_sort/{title}.{ext}")

    def test_parent_directory_segment_is_refused(self):
        with pytest.raises(TemplateError, match="path segment"):
            parse_template("{author_sort}/../{title}.{ext}")

    def test_fat32_illegal_literal_is_refused(self):
        with pytest.raises(TemplateError, match="illegal in a FAT32 filename"):
            parse_template("{author_sort}: {title}.{ext}")

    def test_fat32_illegal_literal_is_allowed_when_fat32_is_off(self):
        assert parse_template("{author_sort}: {title}.{ext}", fat32=False)


class TestRenderPath:
    def test_default_template_nests_by_author(self):
        assert path_of() == "Herbert, Frank/Dune.epub"

    def test_default_template_nests_a_series(self):
        assert path_of(title="Dune Messiah", series="Dune", series_index=2.0) == (
            "Herbert, Frank/Dune/02 - Dune Messiah.epub"
        )

    def test_half_index_in_series(self):
        assert path_of(title="Interlude", series="Saga", series_index=2.5) == (
            "Herbert, Frank/Saga/02.5 - Interlude.epub"
        )

    def test_series_index_is_dropped_without_a_series(self):
        # Calibre defaults every standalone book to index 1.0.
        assert path_of(series_index=1.0) == "Herbert, Frank/Dune.epub"

    def test_flat_template_still_works(self):
        assert path_of(FLAT, title="Dune Messiah", series="Dune", series_index=2.0) == (
            "Herbert, Frank - Dune 02 - Dune Messiah.epub"
        )

    def test_flat_template_without_a_series(self):
        assert path_of(FLAT) == "Herbert, Frank - Dune.epub"

    def test_year_field(self):
        assert path_of("{year}/{title}.{ext}", year=1965) == "1965/Dune.epub"

    def test_year_is_dropped_when_unknown(self):
        assert path_of("{year:||/}{title}.{ext}") == "Dune.epub"

    def test_title_sort_falls_back_to_the_title(self):
        assert path_of("{title_sort}.{ext}") == "Dune.epub"
        assert path_of("{title_sort}.{ext}", title_sort="Dune, The") == "Dune, The.epub"

    def test_id_field_is_available(self):
        assert path_of("{id} - {title}.{ext}", book_id=42) == "42 - Dune.epub"

    def test_colon_in_a_value_is_sanitized(self):
        assert path_of(title="Dune: House Atreides") == "Herbert, Frank/Dune_ House Atreides.epub"

    def test_a_value_cannot_invent_a_path_component(self):
        assert path_of(title="A/B") == "Herbert, Frank/A_B.epub"

    def test_empty_components_collapse(self):
        assert path_of("{series:||/}{title}.{ext}") == "Dune.epub"

    def test_a_component_never_ends_in_a_dot(self):
        # FAT32 refuses the write, so the author's trailing initial dot goes.
        assert path_of(author_sort="Salinger, J. D.") == "Salinger, J. D/Dune.epub"

    def test_a_trailing_dot_survives_when_fat32_is_off(self):
        assert path_of(author_sort="Salinger, J. D.", fat32=False) == ("Salinger, J. D./Dune.epub")

    def test_long_title_is_truncated_to_the_cap(self):
        components = path_of(title="T" + "o" * 400).split("/")
        assert utf16_length(components[-1]) == MAX
        assert components[-1].endswith(".epub")

    def test_every_component_is_capped_independently(self):
        components = path_of(author_sort="A" * 400, title="T" * 400).split("/")
        assert [utf16_length(component) for component in components] == [MAX, MAX]

    def test_truncation_preserves_the_series_prefix_and_extension(self):
        rendered = path_of(title="T" + "o" * 400, series="Saga", series_index=2.5)
        name = rendered.rpartition("/")[2]
        assert name.startswith("02.5 - ")
        assert name.endswith(".epub")


class TestAssignPaths:
    def paths(self, books, template: str = DEFAULT_PATH_TEMPLATE, max_length: int = MAX):
        assigned = assign_paths(books, parse_template(template), max_length=max_length)
        return {book_id: "/".join(components) for book_id, components in assigned.items()}

    def test_non_colliding_paths_stay_clean(self):
        assigned = self.paths([naming(book_id=1), naming(book_id=2, title="Dune Messiah")])
        assert assigned[1] == "Herbert, Frank/Dune.epub"
        assert assigned[2] == "Herbert, Frank/Dune Messiah.epub"

    def test_only_colliding_paths_get_an_id_suffix(self):
        books = [
            naming(book_id=1, title="Dune"),
            naming(book_id=2, title="Dune"),
            naming(book_id=3, title="Unique"),
        ]
        assigned = self.paths(books)
        assert assigned[1] == "Herbert, Frank/Dune (1).epub"
        assert assigned[2] == "Herbert, Frank/Dune (2).epub"
        assert assigned[3] == "Herbert, Frank/Unique.epub"

    def test_collisions_are_detected_case_insensitively(self):
        # Distinct over WebDAV, but the same file on a Kobo's FAT32 storage.
        assigned = self.paths([naming(book_id=1, title="Dune"), naming(book_id=2, title="DUNE")])
        assert assigned[1].endswith("(1).epub")
        assert assigned[2].endswith("(2).epub")

    def test_same_title_under_different_authors_does_not_collide(self):
        books = [
            naming(book_id=1, author_sort="Herbert, Frank"),
            naming(book_id=2, author_sort="Bolaño, Roberto"),
        ]
        assigned = self.paths(books)
        assert assigned[1] == "Herbert, Frank/Dune.epub"
        assert assigned[2] == "Bolaño, Roberto/Dune.epub"

    def test_titles_colliding_only_after_truncation_are_disambiguated(self):
        long_title = "Same " + "x" * 400
        books = [naming(book_id=1, title=long_title), naming(book_id=2, title=long_title)]
        assigned = self.paths(books)
        assert assigned[1] != assigned[2]

    def test_id_suffix_survives_truncation(self):
        long_title = "Same " + "x" * 400
        books = [naming(book_id=1, title=long_title), naming(book_id=2, title=long_title)]
        assigned = self.paths(books)
        assert assigned[1].endswith(" (1).epub")
        assert utf16_length(assigned[1].rpartition("/")[2]) == MAX

    def test_assigned_paths_are_unique(self):
        books = [naming(book_id=index) for index in range(1, 6)]
        assert len(set(self.paths(books).values())) == 5
