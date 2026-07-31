"""Configuration comes entirely from the environment."""

import os

import pytest

from calibre_webdav.config import Config, ConfigError

BASE = {
    "CW_LIBRARY_ROOT": "/library",
    "CW_USERNAME": "user",
    "CW_PASSWORD": "pass",
}


def config(**overrides) -> Config:
    return Config.from_env(BASE | overrides)


class TestRequired:
    def test_library_root_is_required(self):
        with pytest.raises(ConfigError, match="CW_LIBRARY_ROOT"):
            Config.from_env({"CW_USERNAME": "u", "CW_PASSWORD": "p"})

    def test_credentials_are_required_by_default(self):
        with pytest.raises(ConfigError, match="CW_USERNAME"):
            Config.from_env({"CW_LIBRARY_ROOT": "/library"})

    def test_anonymous_access_must_be_opted_into(self):
        parsed = Config.from_env({"CW_LIBRARY_ROOT": "/library", "CW_ALLOW_ANONYMOUS": "true"})
        assert parsed.allow_anonymous is True

    def test_database_path_is_derived_from_the_root(self):
        assert config().database_path.name == "metadata.db"


class TestDefaults:
    def test_sensible_defaults(self):
        parsed = config()
        assert parsed.host == "0.0.0.0"
        assert parsed.port == 8080
        assert parsed.format_preference == ("epub", "pdf")
        assert parsed.max_filename_length == 200
        assert parsed.allow_anonymous is False


class TestParsing:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_booleans(self, value):
        assert config(CW_ALLOW_ANONYMOUS=value).allow_anonymous is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_falsy_booleans(self, value):
        assert config(CW_ALLOW_ANONYMOUS=value).allow_anonymous is False

    def test_invalid_boolean_is_rejected(self):
        with pytest.raises(ConfigError, match="not a boolean"):
            config(CW_ALLOW_ANONYMOUS="maybe")

    def test_format_preference_is_normalized(self):
        assert config(CW_FORMAT_PREFERENCE=" .EPUB , Pdf ,mobi").format_preference == (
            "epub",
            "pdf",
            "mobi",
        )

    def test_repeated_format_is_rejected(self):
        with pytest.raises(ConfigError, match="repeats format"):
            config(CW_FORMAT_PREFERENCE="epub,epub")

    def test_port_out_of_range_is_rejected(self):
        with pytest.raises(ConfigError, match="out of range"):
            config(CW_PORT="70000")

    def test_non_numeric_port_is_rejected(self):
        with pytest.raises(ConfigError, match="not an integer"):
            config(CW_PORT="http")

    def test_replacement_must_itself_be_legal(self):
        with pytest.raises(ConfigError, match="illegal"):
            config(CW_SANITIZE_REPLACEMENT="/")

    def test_replacement_preserves_significant_whitespace(self):
        assert config(CW_SANITIZE_REPLACEMENT=" - ").sanitize_replacement == " - "

    def test_password_whitespace_is_preserved(self):
        assert config(CW_PASSWORD=" spaced ").password == " spaced "


class TestValidate:
    def test_missing_library_root_is_reported(self, tmp_path):
        with pytest.raises(ConfigError, match="does not exist"):
            config(CW_LIBRARY_ROOT=str(tmp_path / "nope")).validate()

    def test_library_root_that_is_a_file_is_reported(self, tmp_path):
        target = tmp_path / "afile"
        target.write_text("")
        with pytest.raises(ConfigError, match="not a directory"):
            config(CW_LIBRARY_ROOT=str(target)).validate()

    def test_missing_database_is_reported(self, tmp_path):
        with pytest.raises(ConfigError, match="no Calibre database"):
            config(CW_LIBRARY_ROOT=str(tmp_path)).validate()

    def test_valid_library_passes(self, library):
        library.config().validate()

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_unreadable_library_names_the_uid(self, library):
        # The container-uid-mismatch case. It must not be reported as a missing
        # or malformed path, which is where the naive check sends you.
        library.root.chmod(0o000)
        try:
            with pytest.raises(ConfigError, match="not readable by uid=") as caught:
                library.config().validate()
        finally:
            library.root.chmod(0o755)
        assert "--user" in str(caught.value)

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_unreadable_database_names_the_uid(self, library):
        library.database_path.chmod(0o000)
        try:
            with pytest.raises(ConfigError, match="not readable by uid="):
                library.config().validate()
        finally:
            library.database_path.chmod(0o644)
