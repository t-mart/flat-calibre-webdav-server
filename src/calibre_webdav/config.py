"""Environment-driven configuration.

Every knob is an environment variable so the server can be configured from a
container without shipping a config file. `Config.from_env` does all parsing and
consistency checking that does not touch the filesystem; `Config.validate`
does the filesystem checks separately so the parsing half stays trivially
testable.
"""

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .naming import PathTemplate, TemplateError, parse_template

DEFAULT_FORMAT_PREFERENCE = ("epub", "pdf")

# One directory per author, one per series, the series index leading the title so
# a series sorts into reading order. Standalone books land beside the series
# directories because the whole `{series...}` group renders empty without one.
DEFAULT_PATH_TEMPLATE = "{author_sort}/{series:||/}{series_index:|| - }{title}.{ext}"

# FAT32 long filenames cap at 255 UTF-16 units. We stay well under it: KOReader
# writes a `<stem>.sdr` sidecar directory next to each book and puts its own
# files inside, so the book name is not the only thing competing for path budget.
DEFAULT_MAX_FILENAME_LENGTH = 200


class ConfigError(ValueError):
    """Raised when the environment does not describe a runnable server."""


@dataclass(frozen=True, slots=True)
class Config:
    library_root: Path
    host: str
    port: int
    username: str | None
    password: str | None
    allow_anonymous: bool
    format_preference: tuple[str, ...]
    path_template: PathTemplate
    max_filename_length: int
    fat32: bool
    index_debounce_seconds: float
    db_timeout_seconds: float
    db_retry_attempts: int
    verbose: int

    @property
    def database_path(self) -> Path:
        return self.library_root / "metadata.db"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        allow_anonymous = _env_bool(env, "CW_ALLOW_ANONYMOUS", False)
        # Credentials are taken verbatim: stripping would silently mangle a
        # password with meaningful leading or trailing whitespace.
        username = env.get("CW_USERNAME") or None
        password = env.get("CW_PASSWORD") or None
        if not allow_anonymous and not (username and password):
            raise ConfigError(
                "CW_USERNAME and CW_PASSWORD are required. "
                "Set CW_ALLOW_ANONYMOUS=true to serve without authentication."
            )

        fat32 = _env_bool(env, "CW_FAT32", True)

        return cls(
            library_root=Path(_require(env, "CW_LIBRARY_ROOT")).expanduser(),
            host=_env_str(env, "CW_HOST", "0.0.0.0") or "0.0.0.0",
            port=_env_int(env, "CW_PORT", 8080, minimum=1, maximum=65535),
            username=username,
            password=password,
            allow_anonymous=allow_anonymous,
            format_preference=_env_formats(env, "CW_FORMAT_PREFERENCE"),
            path_template=_env_template(env, "CW_PATH_TEMPLATE", fat32),
            max_filename_length=_env_int(
                env,
                "CW_MAX_FILENAME_LENGTH",
                DEFAULT_MAX_FILENAME_LENGTH,
                minimum=16,
                maximum=255,
            ),
            fat32=fat32,
            index_debounce_seconds=_env_float(env, "CW_INDEX_DEBOUNCE_SECONDS", 5.0, minimum=0.0),
            db_timeout_seconds=_env_float(env, "CW_DB_TIMEOUT_SECONDS", 5.0, minimum=0.1),
            db_retry_attempts=_env_int(env, "CW_DB_RETRY_ATTEMPTS", 3, minimum=1, maximum=10),
            verbose=_env_int(env, "CW_VERBOSE", 3, minimum=0, maximum=5),
        )

    def validate(self) -> None:
        """Check the library exists and is readable by this process.

        Permission failures are called out separately from missing paths: in a
        container the usual cause is the uid not matching whoever owns the
        library on the host, and `Path.is_dir()` reports that as a plain False,
        which sends you looking in entirely the wrong place.
        """
        mode = _stat_mode(self.library_root, "CW_LIBRARY_ROOT")
        if not stat.S_ISDIR(mode):
            raise ConfigError(f"CW_LIBRARY_ROOT={self.library_root} is not a directory")
        if not os.access(self.library_root, os.R_OK | os.X_OK):
            raise ConfigError(_unreadable_message(self.library_root))

        mode = _stat_mode(self.database_path, "metadata.db")
        if not stat.S_ISREG(mode):
            raise ConfigError(f"{self.database_path} is not a regular file")
        if not os.access(self.database_path, os.R_OK):
            raise ConfigError(_unreadable_message(self.database_path))


def _stat_mode(path: Path, label: str) -> int:
    """Stat a path, turning the two interesting failures into clear errors."""
    try:
        return os.stat(path).st_mode
    except FileNotFoundError:
        if label == "metadata.db":
            raise ConfigError(
                f"no Calibre database at {path}. CW_LIBRARY_ROOT must point at the library root."
            ) from None
        raise ConfigError(f"{label}={path} does not exist") from None
    except PermissionError:
        raise ConfigError(_unreadable_message(path)) from None


def _unreadable_message(path: Path) -> str:
    return (
        f"{path} is not readable by uid={os.getuid()} gid={os.getgid()} "
        f"groups={sorted(os.getgroups())}. Under Docker, set --user to an id that "
        "can read the library. Add --group-add for each supplementary group that "
        "the id needs."
    )


def _require(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is required")
    return value


def _env_str(env: Mapping[str, str], key: str, default: str | None) -> str | None:
    value = env.get(key)
    return default if value is None else value.strip()


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None or not value.strip():
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key}={value!r} is not a boolean")


def _env_int(
    env: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = env.get(key)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        raise ConfigError(f"{key}={value!r} is not an integer") from None
    if parsed < minimum or (maximum is not None and parsed > maximum):
        bound = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ConfigError(f"{key}={parsed} is out of range ({bound})")
    return parsed


def _env_float(env: Mapping[str, str], key: str, default: float, *, minimum: float) -> float:
    value = env.get(key)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value.strip())
    except ValueError:
        raise ConfigError(f"{key}={value!r} is not a number") from None
    if parsed < minimum:
        raise ConfigError(f"{key}={parsed} is out of range (>= {minimum})")
    return parsed


def _env_template(env: Mapping[str, str], key: str, fat32: bool) -> PathTemplate:
    text = _env_str(env, key, DEFAULT_PATH_TEMPLATE) or DEFAULT_PATH_TEMPLATE
    try:
        return parse_template(text, fat32=fat32)
    except TemplateError as error:
        raise ConfigError(f"{key}={text!r}: {error}") from None


def _env_formats(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    """Parse a comma-separated format preference list, most preferred first."""
    value = env.get(key)
    if value is None or not value.strip():
        return DEFAULT_FORMAT_PREFERENCE
    formats = tuple(
        part.strip().casefold().lstrip(".") for part in value.split(",") if part.strip()
    )
    if not formats:
        raise ConfigError(f"{key}={value!r} lists no formats")
    duplicates = {fmt for fmt in formats if formats.count(fmt) > 1}
    if duplicates:
        raise ConfigError(f"{key} repeats format(s): {', '.join(sorted(duplicates))}")
    return formats
