"""Configuration schema, normalization and validation.

The :class:`Config` dataclass is the single source of truth for every
option: each field carries its TOML group, CLI override, normalizer and
validation rules via :func:`option`. The CLI parser, config validation,
the canonical flat form and the TOML layout written by ``config migrate``
are all derived from this schema.

:func:`canonicalize` maps an in-memory config (grouped mappings or internal
flat ``"group.key"`` names) onto one flat canonical dict before any merging
happens. TOML files must use nested tables or unquoted dotted keys; literal
dotted keys are rejected at the file boundary. Legacy JSON configs are
translated into the current format first by :func:`convert_legacy_config`
(see the legacy support section at the bottom of this module); legacy
spellings are not accepted in TOML configs, but validation points from them
to the current names.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Literal, TypeAlias, cast

from syncmymoodle import pathing
from syncmymoodle.constants import COURSE_PREFIX_HANDLING_OPTIONS, QUIZ_MODES
from syncmymoodle.secret_providers import (
    EXTERNAL_SECRET_PROVIDER_OPTIONS,
    SECRET_PROVIDER_OPTIONS,
)

PatternConfig: TypeAlias = dict[str, list[str]]
ConfigDict: TypeAlias = dict[str, Any]
CliValueKind: TypeAlias = Literal["scalar", "csv", "flag"]

CONFLICT_HANDLING_OPTIONS = ("rename", "keep", "overwrite")
DEFAULT_TOKEN_STORE = "keyring"
DEFAULT_LOGIN_PROVIDER = "prompt"
TOKEN_STORE_OPTIONS = (DEFAULT_TOKEN_STORE, "env-file")
LOGIN_PROVIDER_OPTIONS = (
    DEFAULT_LOGIN_PROVIDER,
    *TOKEN_STORE_OPTIONS,
    *SECRET_PROVIDER_OPTIONS,
)


def identity(value: Any) -> Any:
    return value


def as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        values = value
    else:
        values = [value]
    return [str(item) for item in values if item is not None]


def normalize_role_shortnames(value: Any) -> list[str]:
    return list(
        dict.fromkeys(
            role for item in as_string_list(value) if (role := item.strip().casefold())
        )
    )


def as_command_argv(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def command_argv_error(value: Any) -> str | None:
    if not isinstance(value, list):
        return "must be an array of command arguments, not a shell string"
    if not value:
        return None
    if not all(isinstance(item, str) and item for item in value):
        return "must contain only non-empty string arguments"
    return None


def string_error(value: Any) -> str | None:
    if not isinstance(value, str):
        return f"must be a string, got {value!r}"
    return None


def normalize_pattern_config(value: Any) -> PatternConfig:
    if isinstance(value, Mapping):
        return {
            str(key): patterns
            for key, raw_patterns in value.items()
            if (patterns := as_string_list(raw_patterns))
        }
    patterns = as_string_list(value)
    return {"*": patterns} if patterns else {}


_FILE_SIZE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(i?b)?\s*$",
    re.IGNORECASE,
)
_FILE_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
_MAX_FILE_SIZE = 2**63 - 1


def parse_file_size(value: Any) -> int:
    """Parse a size given in bytes or with a K/M/G/T suffix (e.g. "500M")."""
    if isinstance(value, bool):
        raise ValueError(f"not a file size: {value!r}")
    if isinstance(value, int):
        size = int(value)
    elif isinstance(value, float):
        raise ValueError(f"not a file size: {value!r}")
    else:
        match = _FILE_SIZE_RE.match(str(value))
        if match is None:
            raise ValueError(f"not a file size: {value!r}")
        number = match.group(1)
        unit = match.group(2).lower()
        suffix = (match.group(3) or "").lower()
        if "." in number and not unit:
            raise ValueError(f"not a file size: {value!r}")
        if suffix.startswith("i") and not unit:
            raise ValueError(f"not a file size: {value!r}")
        whole, separator, fraction = number.partition(".")
        numerator = int(whole + fraction) * _FILE_SIZE_UNITS[unit]
        denominator = 10 ** len(fraction) if separator else 1
        size = numerator // denominator
    if size < 0 or size > _MAX_FILE_SIZE:
        raise ValueError(f"not a file size: {value!r}")
    return size


def file_size_error(value: Any) -> str | None:
    if value in (None, "", 0) and not isinstance(value, bool):
        return None
    try:
        parse_file_size(value)
    except ValueError:
        return f"must be a size in bytes or with a K/M/G/T suffix (e.g. '500M'), got {value!r}"
    return None


def default_cookie_file() -> str:
    return os.fspath(pathing.user_config_dir() / "session")


def format_choices(choices: tuple[str, ...]) -> str:
    return ", ".join(repr(choice) for choice in choices)


@dataclass(frozen=True)
class CliOverride:
    arg_name: str
    value_kind: CliValueKind
    help: str
    # Legacy spellings remain accepted but are hidden from --help.
    aliases: tuple[str, ...] = ()
    # Boolean value represented by a legacy flag alias.
    legacy_flag_value: bool = True

    @property
    def dest(self) -> str:
        """Argparse namespace attribute the option is stored under."""
        return self.arg_name.replace("-", "_")


def cli_arg(
    arg_name: str,
    help_text: str,
    aliases: tuple[str, ...] = (),
) -> CliOverride:
    return CliOverride(arg_name, "scalar", help_text, aliases)


def cli_csv(
    arg_name: str, help_text: str, aliases: tuple[str, ...] = ()
) -> CliOverride:
    return CliOverride(arg_name, "csv", help_text, aliases)


def cli_flag(
    arg_name: str,
    help_text: str,
    aliases: tuple[str, ...] = (),
    *,
    legacy_flag_value: bool = True,
) -> CliOverride:
    return CliOverride(arg_name, "flag", help_text, aliases, legacy_flag_value)


def option(
    default: Any = None,
    *,
    group: str,
    key: str | None = None,
    factory: Callable[[], Any] | None = None,
    normalize: Callable[[Any], Any] = identity,
    falsey_uses_default: bool = False,
    choices: tuple[str, ...] = (),
    validate: Callable[[Any], str | None] | None = None,
    cli: CliOverride | None = None,
    resolve_relative_path: bool = False,
    repr: bool = True,
) -> Any:
    """Declare a :class:`Config` field together with its option schema.

    ``key`` is the spelling used inside the option's ``group`` table
    (defaults to the field name); the flat canonical spelling is ``"group.key"``.
    ``validate`` returns an error fragment for values the option's ``normalize`` cannot handle,
    or None when the value is fine.
    """
    metadata = {
        "config": {
            "group": group,
            "key": key,
            "normalize": normalize,
            "falsey_uses_default": falsey_uses_default,
            "choices": choices,
            "validate": validate,
            "cli": cli,
            "resolve_relative_path": resolve_relative_path,
        }
    }
    if factory is not None:
        return field(default_factory=factory, metadata=metadata, repr=repr)
    return field(default=default, metadata=metadata, repr=repr)


@dataclass(frozen=True)
class PromptAuthSource:
    pass


@dataclass(frozen=True)
class KeyringAuthSource:
    store_totp_secret: bool


@dataclass(frozen=True)
class EnvFileAuthSource:
    path: Path


@dataclass(frozen=True)
class ExternalAuthSource:
    provider: str
    password_reference: str
    otp_reference: str | None


@dataclass(frozen=True)
class CommandAuthSource:
    password_command: tuple[str, ...]
    otp_command: tuple[str, ...]


AuthSource: TypeAlias = (
    PromptAuthSource
    | KeyringAuthSource
    | EnvFileAuthSource
    | ExternalAuthSource
    | CommandAuthSource
)


@dataclass(frozen=True)
class Config:
    """Typed view of the user configuration.

    ``from_dict`` accepts the current config shape (grouped tables or flat
    canonical keys); missing keys keep the field defaults declared here.
    Legacy JSON shapes must be translated with :func:`convert_legacy_config`
    first.
    """

    # Credentials / login
    user: str | None = option(
        group="auth",
        validate=string_error,
        cli=cli_arg("user", "set your RWTH Single Sign-On username"),
    )
    token_store: str = option(
        DEFAULT_TOKEN_STORE,
        group="auth.tokens",
        key="store",
        falsey_uses_default=True,
        choices=TOKEN_STORE_OPTIONS,
        validate=string_error,
    )
    token_env_file: str | None = option(
        group="auth.tokens",
        key="env_file",
        falsey_uses_default=True,
        validate=string_error,
        resolve_relative_path=True,
    )
    login_provider: str = option(
        DEFAULT_LOGIN_PROVIDER,
        group="auth.login",
        key="provider",
        falsey_uses_default=True,
        choices=LOGIN_PROVIDER_OPTIONS,
        validate=string_error,
    )
    totp_serial: str | None = option(
        group="auth.login",
        validate=string_error,
        cli=cli_arg(
            "totp-serial",
            "set the serial number of your RWTH TOTP method, e.g. "
            "TOTP12345678 (not the current 6-digit code; find it in "
            "the RWTH IDM Token Manager)",
            aliases=("totp",),
        ),
    )
    keyring_store_totp_secret: bool = option(
        False,
        group="auth.login",
        normalize=bool,
        cli=cli_flag(
            "keyring-store-totp-secret",
            "store the TOTP seed when the configured RWTH sign-in method is "
            "the system keyring",
            aliases=("secretservicetotpsecret",),
        ),
    )
    login_env_file: str | None = option(
        group="auth.login",
        key="env_file",
        falsey_uses_default=True,
        validate=string_error,
        resolve_relative_path=True,
        cli=cli_arg(
            "login-env-file",
            "temporarily use a protected environment file for the RWTH password "
            "and optional TOTP seed",
        ),
    )
    secret_password_ref: str | None = option(
        group="auth.login",
        key="password",
        falsey_uses_default=True,
        validate=string_error,
    )
    secret_otp_ref: str | None = option(
        group="auth.login",
        key="otp",
        falsey_uses_default=True,
        validate=string_error,
    )
    secret_password_command: list[str] = option(
        group="auth.login",
        key="password_command",
        factory=list,
        normalize=as_command_argv,
        validate=command_argv_error,
    )
    secret_otp_command: list[str] = option(
        group="auth.login",
        key="otp_command",
        factory=list,
        normalize=as_command_argv,
        validate=command_argv_error,
    )
    # Local paths
    sync_directory: str = option(
        "./",
        group="paths",
        falsey_uses_default=True,
        validate=string_error,
        resolve_relative_path=True,
        cli=cli_arg(
            "sync-directory",
            "set the directory to sync Moodle files to",
            aliases=("basedir",),
        ),
    )
    cookie_file: str = option(
        factory=default_cookie_file,
        group="paths",
        falsey_uses_default=True,
        validate=string_error,
        resolve_relative_path=True,
        cli=cli_arg(
            "cookie-file",
            "set the file used to cache the RWTH browser session",
            aliases=("cookiefile",),
        ),
    )
    # Explicit path to a Chromium-family browser used to render quiz PDFs. When
    # unset, the browser is auto-discovered (see quiz.find_chromium).
    browser: str | None = option(
        group="paths",
        falsey_uses_default=True,
        validate=string_error,
        resolve_relative_path=True,
        cli=cli_arg(
            "browser",
            "set the path to a Chrome/Chromium/Edge binary for quiz PDF rendering",
            aliases=("chromiumpath",),
        ),
    )

    # Course/semester selection and naming
    selected_courses: list[str] = option(
        group="courses",
        key="selected",
        factory=list,
        normalize=as_string_list,
        cli=cli_csv(
            "courses",
            "sync only these comma-separated Moodle course URLs or numeric IDs; "
            "defaults to all courses unless --semesters is set",
        ),
    )
    skip_courses: list[str] = option(
        group="courses",
        key="skip",
        factory=list,
        normalize=as_string_list,
        cli=cli_csv(
            "skip-courses",
            "exclude these comma-separated Moodle course URLs or numeric IDs; "
            "ignored when --courses is set",
            aliases=("skipcourses",),
        ),
    )
    exclude_course_roles: list[str] = option(
        group="courses",
        key="exclude_roles",
        factory=list,
        normalize=normalize_role_shortnames,
        cli=cli_csv(
            "exclude-course-roles",
            "exclude courses where one of your directly assigned Moodle course "
            "roles has one of these comma-separated shortnames, e.g. tutor; "
            "ignored when --courses is set",
        ),
    )
    only_sync_semester: list[str] = option(
        group="courses",
        key="semesters",
        factory=list,
        normalize=as_string_list,
        cli=cli_csv(
            "semesters",
            "sync only these comma-separated semester IDs, e.g. 25ws,26ss; "
            "ignored when --courses is set",
            aliases=("semester",),
        ),
    )
    course_prefix_handling: str = option(
        "keep",
        group="courses",
        key="prefix_handling",
        falsey_uses_default=True,
        choices=COURSE_PREFIX_HANDLING_OPTIONS,
        validate=string_error,
        cli=cli_arg(
            "course-prefix-handling",
            "handle leading two-character course prefixes in local folder "
            "names: 'keep', 'remove', or 'suffix' (used by setup)",
            aliases=("courseprefix",),
        ),
    )

    # Download behaviour
    update_files: bool = option(
        False,
        group="downloads",
        normalize=bool,
        cli=cli_flag(
            "update-files",
            "redownload remote files that changed without changing name or path",
            aliases=("updatefiles",),
        ),
    )
    conflict_handling: str = option(
        "rename",
        group="downloads",
        falsey_uses_default=True,
        choices=CONFLICT_HANDLING_OPTIONS,
        validate=string_error,
        cli=cli_arg(
            "conflict-handling",
            "choose how to handle locally modified files when updating: "
            "'rename' (default) moves the old file aside, 'keep' skips the "
            "update, 'overwrite' replaces the local file",
            aliases=("updatefilesconflict",),
        ),
    )
    # Reporting-only mode: sync and list what would be downloaded but never
    # write files or caches (see downloader/quiz dry_run checks).
    dry_run: bool = option(
        False,
        group="downloads",
        normalize=bool,
        cli=cli_flag(
            "dry-run",
            "only report what would be downloaded, without writing any files",
        ),
    )

    # Exclude/allow rules
    allowed_domains: PatternConfig = option(
        group="filters",
        factory=dict,
        normalize=normalize_pattern_config,
        cli=cli_csv(
            "allowed-domains",
            "only keep discovered links on these comma-separated domains",
            aliases=("alloweddomains",),
        ),
    )
    # Byte limits for downloads (None/0 = no limit). Applied where a size is
    # known up front: direct downloads with a Content-Length and YouTube
    # videos whose size yt-dlp can estimate before downloading.
    max_file_size: int | None = option(
        group="filters",
        normalize=parse_file_size,
        falsey_uses_default=True,
        validate=file_size_error,
        cli=cli_arg(
            "max-file-size",
            "skip files whose size is known to exceed this limit, e.g. '500M' or '2G'",
        ),
    )
    min_file_size: int | None = option(
        group="filters",
        normalize=parse_file_size,
        falsey_uses_default=True,
        validate=file_size_error,
        cli=cli_arg(
            "min-file-size",
            "skip files whose size is known to be below this limit, e.g. '10K'",
        ),
    )
    exclude_filetypes: list[str] = option(
        group="filters",
        factory=list,
        normalize=as_string_list,
        cli=cli_csv(
            "exclude-filetypes",
            "exclude these comma-separated file extensions, e.g. mp4,mkv",
            aliases=("excludefiletypes",),
        ),
    )
    exclude_files: list[str] = option(
        group="filters",
        factory=list,
        normalize=as_string_list,
        cli=cli_csv(
            "exclude-files",
            'exclude specific files using comma-separated patterns e.g. "*.bak,*.tmp"',
            aliases=("excludefiles",),
        ),
    )
    exclude_links: PatternConfig = option(
        group="filters",
        factory=dict,
        normalize=normalize_pattern_config,
        cli=cli_csv(
            "exclude-links",
            "exclude discovered links using comma-separated URL patterns",
            aliases=("excludelinks",),
        ),
    )
    exclude_sections: PatternConfig = option(
        group="filters",
        factory=dict,
        normalize=normalize_pattern_config,
        cli=cli_csv(
            "exclude-sections",
            "exclude Moodle sections by comma-separated names, IDs, or patterns",
            aliases=("excludesections",),
        ),
    )
    exclude_modules: PatternConfig = option(
        group="filters",
        factory=dict,
        normalize=normalize_pattern_config,
        cli=cli_csv(
            "exclude-modules",
            "exclude Moodle modules by comma-separated names, IDs, types, "
            "URLs or patterns",
            aliases=("excludemodules",),
        ),
    )

    # Link inspection and link-based content sources. follow_links replaces
    # the legacy no_links/nolinks toggle with inverted meaning (see
    # convert_legacy_config); setting it to false disables all of [links].
    follow_links: bool = option(
        True,
        group="links",
        normalize=bool,
        cli=cli_flag(
            "follow-links",
            "inspect links found in Moodle pages for linked content such as "
            "YouTube and Opencast videos",
            aliases=("nolinks",),
            legacy_flag_value=False,
        ),
    )
    link_youtube: bool = option(
        True,
        group="links",
        key="youtube",
        normalize=bool,
        cli=cli_flag(
            "youtube",
            "include YouTube links and embeds",
        ),
    )
    link_opencast: bool = option(
        True,
        group="links",
        key="opencast",
        normalize=bool,
        cli=cli_flag(
            "opencast",
            "include Opencast links and embeds",
        ),
    )
    link_sciebo: bool = option(
        True,
        group="links",
        key="sciebo",
        normalize=bool,
        cli=cli_flag(
            "sciebo",
            "include Sciebo links",
        ),
    )
    link_emedia: bool = option(
        True,
        group="links",
        key="emedia",
        normalize=bool,
        cli=cli_flag(
            "emedia",
            "include videos from the emedia Medizin VEIRA service",
        ),
    )

    # Moodle activity types. Keys omitted from a [modules] table keep these
    # defaults; legacy used_modules trees instead disable omitted entries
    # (see convert_legacy_config).
    module_assignment: bool = option(
        True, group="modules", key="assignment", normalize=bool
    )
    module_resource: bool = option(
        True, group="modules", key="resource", normalize=bool
    )
    module_folder: bool = option(True, group="modules", key="folder", normalize=bool)
    quiz_mode: str = option(
        "html",
        group="modules",
        key="quiz",
        choices=QUIZ_MODES,
        validate=string_error,
        cli=cli_arg("quiz", "save quiz attempts as 'off', 'html', 'pdf', or 'both'"),
    )

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any] | None,
        *,
        config_path: Path | None = None,
    ) -> "Config":
        canonical = canonicalize(raw)
        errors = config_validation_errors(canonical, config_path=config_path)
        if errors:
            raise ConfigValidationError(config_path, errors)
        kwargs: dict[str, Any] = {}
        for opt in CONFIG_OPTIONS:
            if opt.canonical_key not in canonical:
                continue
            value = canonical[opt.canonical_key]
            if opt.falsey_uses_default and not value:
                continue
            kwargs[opt.field_name] = opt.normalize(value)
        return cls(**kwargs)

    @property
    def auth_source(self) -> AuthSource:
        """Return the configured RWTH sign-in method."""
        if self.login_provider == "keyring":
            return KeyringAuthSource(self.keyring_store_totp_secret)
        if self.login_provider == "env-file":
            assert self.login_env_file is not None
            return EnvFileAuthSource(Path(self.login_env_file))
        if self.login_provider == "command":
            return CommandAuthSource(
                tuple(self.secret_password_command),
                tuple(self.secret_otp_command),
            )
        if self.login_provider not in {"prompt", "keyring", "env-file"}:
            return ExternalAuthSource(
                self.login_provider,
                str(self.secret_password_ref),
                self.secret_otp_ref,
            )
        return PromptAuthSource()

    def link_source_enabled(self, name: str) -> bool:
        """Whether a configured link-based content source is enabled.

        All sources require link inspection (follow_links) to be on.
        """
        flags = {
            "youtube": self.link_youtube,
            "opencast": self.link_opencast,
            "sciebo": self.link_sciebo,
            "emedia": self.link_emedia,
        }
        return self.follow_links and flags.get(name, False)

    def matching_excluded_course_role(
        self, role_shortnames: Iterable[str] | None
    ) -> str | None:
        """Return the first configured role matching Moodle's direct assignments."""
        if role_shortnames is None:
            return None
        normalized = set(normalize_role_shortnames(list(role_shortnames)))
        return next(
            (role for role in self.exclude_course_roles if role in normalized),
            None,
        )


@dataclass(frozen=True)
class ConfigOption:
    field_name: str
    group: str
    key: str
    canonical_key: str
    normalize: Callable[[Any], Any]
    falsey_uses_default: bool
    choices: tuple[str, ...]
    validate: Callable[[Any], str | None] | None
    cli: CliOverride | None
    resolve_relative_path: bool


def _build_config_options() -> tuple[ConfigOption, ...]:
    options = []
    for config_field in fields(Config):
        if "config" not in config_field.metadata:
            continue
        meta = cast(dict[str, Any], config_field.metadata["config"])
        group = cast(str, meta["group"])
        key = meta["key"] or config_field.name
        options.append(
            ConfigOption(
                field_name=config_field.name,
                group=group,
                key=key,
                canonical_key=f"{group}.{key}",
                normalize=meta["normalize"],
                falsey_uses_default=meta["falsey_uses_default"],
                choices=meta["choices"],
                validate=meta["validate"],
                cli=meta["cli"],
                resolve_relative_path=meta["resolve_relative_path"],
            )
        )
    return tuple(options)


CONFIG_OPTIONS = _build_config_options()
_CANONICAL_KEYS = frozenset(opt.canonical_key for opt in CONFIG_OPTIONS)
_GROUP_PATHS = frozenset(opt.group for opt in CONFIG_OPTIONS)
_SCHEMA_PATHS = _CANONICAL_KEYS | _GROUP_PATHS
# key spelling inside a group table -> canonical key.
_GROUP_MEMBER_KEYS: dict[str, dict[str, str]] = {}
for _opt in CONFIG_OPTIONS:
    _GROUP_MEMBER_KEYS.setdefault(_opt.group, {})[_opt.key] = _opt.canonical_key

# Corpus for did-you-mean suggestions; sorted so ties resolve deterministically.
_SUGGESTION_KEYS = sorted(_CANONICAL_KEYS | _GROUP_PATHS)


def canonicalize(raw: Mapping[str, Any] | None) -> ConfigDict:
    """Normalize a current-format config into the flat canonical dict.

    Expands group tables into flat ``"group.key"`` names; already-flat
    canonical keys pass through, so the function is idempotent. Unknown
    keys are kept under the spelling the user wrote (dotted with their
    group path) so validation can report them faithfully.
    """
    flat: ConfigDict = {}
    _flatten_into(flat, raw or {}, "")
    return flat


def literal_dotted_toml_key_errors(raw: Mapping[str, Any]) -> list[str]:
    """Reject quoted TOML keys that imitate the internal flat config form.

    TOML dotted keys such as ``auth.user`` parse into nested mappings and
    are valid. Quoted keys such as ``"auth.user"`` remain one literal key;
    accepting those would give config mutation commands two syntax trees for
    the same canonical setting.
    """
    errors: list[str] = []

    def collect(mapping: Mapping[str, Any], group: str) -> None:
        for raw_key, value in mapping.items():
            key = str(raw_key)
            path = f"{group}.{key}" if group else key
            if "." in key and path in _SCHEMA_PATHS:
                errors.append(
                    f"literal dotted TOML key {path!r} is not supported; "
                    "use nested tables or unquoted dotted keys"
                )
                continue
            if path in _GROUP_PATHS and isinstance(value, Mapping):
                collect(value, path)

    collect(raw, "")
    return errors


def _resolve_config_path_value(value: Any, base_dir: Path) -> Any:
    if not value or not isinstance(value, (str, os.PathLike)):
        return value
    path_value = Path(cast(str | os.PathLike[str], value))
    return os.fspath(pathing.absolute_path(path_value, base_dir))


def resolve_relative_path_options(
    raw: Mapping[str, Any],
    base_dir: Path,
) -> ConfigDict:
    config = dict(canonicalize(raw))
    for opt in CONFIG_OPTIONS:
        if opt.resolve_relative_path and opt.canonical_key in config:
            config[opt.canonical_key] = _resolve_config_path_value(
                config[opt.canonical_key],
                base_dir,
            )
    return config


def _flatten_into(flat: ConfigDict, mapping: Mapping[str, Any], group: str) -> None:
    members = _GROUP_MEMBER_KEYS.get(group, {})
    for raw_key, value in mapping.items():
        key = str(raw_key)
        path = f"{group}.{key}" if group else key
        if path in _GROUP_PATHS and isinstance(value, Mapping):
            _flatten_into(flat, value, path)
        elif key in members:
            flat[members[key]] = value
        else:
            flat[path] = value


class ConfigValidationError(ValueError):
    def __init__(self, path: Path | None, errors: list[str]):
        self.path = path
        self.errors = errors
        location = f" in {path}" if path else ""
        details = "\n".join(f"- {error}" for error in errors)
        super().__init__(f"invalid config{location}:\n{details}")


def config_validation_errors(
    raw: Mapping[str, Any],
    *,
    config_path: Path | None = None,
) -> list[str]:
    canonical = canonicalize(raw)
    errors = [
        unknown_config_key_error(key)
        for key in sorted(set(canonical) - _CANONICAL_KEYS)
    ]
    for opt in CONFIG_OPTIONS:
        if opt.canonical_key in canonical:
            errors.extend(option_value_errors(opt, canonical[opt.canonical_key]))
    errors.extend(size_limit_errors(canonical))
    errors.extend(auth_source_errors(canonical))
    errors.extend(managed_path_errors(canonical, config_path))
    return errors


def size_limit_errors(canonical: ConfigDict) -> list[str]:
    limits: dict[str, int] = {}
    for key in ("filters.min_file_size", "filters.max_file_size"):
        value = canonical.get(key)
        if value in (None, "", 0) and not isinstance(value, bool):
            continue
        try:
            limits[key] = parse_file_size(value)
        except ValueError:
            continue
    min_size = limits.get("filters.min_file_size")
    max_size = limits.get("filters.max_file_size")
    if min_size is None or max_size is None or min_size <= max_size:
        return []
    return ["filters.min_file_size must not exceed filters.max_file_size"]


def managed_path_errors(
    canonical: ConfigDict,
    config_path: Path | None = None,
) -> list[str]:
    managed_paths: list[tuple[str, Any]] = []
    if config_path is not None:
        managed_paths.append(("configuration file", config_path))
    managed_paths.append(
        (
            "paths.cookie_file",
            canonical.get("paths.cookie_file") or default_cookie_file(),
        )
    )
    if (canonical.get("auth.login.provider") or DEFAULT_LOGIN_PROVIDER) == "env-file":
        managed_paths.append(
            ("auth.login.env_file", canonical.get("auth.login.env_file"))
        )
    if (canonical.get("auth.tokens.store") or DEFAULT_TOKEN_STORE) == "env-file":
        managed_paths.append(
            ("auth.tokens.env_file", canonical.get("auth.tokens.env_file"))
        )

    errors: list[str] = []
    seen: dict[tuple[bool, str], str] = {}
    for label, value in managed_paths:
        identity = pathing.path_identity(value)
        if identity is None:
            continue
        previous = seen.get(identity)
        if previous is not None:
            errors.append(
                f"{label} must not resolve to the same path as {previous}; "
                "configure separate files"
            )
        else:
            seen[identity] = label
    return errors


def _login_reference_errors(canonical: ConfigDict, provider: Any) -> list[str]:
    errors: list[str] = []
    password_ref = canonical.get("auth.login.password")
    otp_ref = canonical.get("auth.login.otp")
    password_command = canonical.get("auth.login.password_command")
    otp_command = canonical.get("auth.login.otp_command")
    if provider == "command":
        if not password_command:
            errors.append(
                "auth.login.password_command is required when "
                "auth.login.provider is 'command'"
            )
        if password_ref or otp_ref:
            errors.append(
                "auth.login.password and auth.login.otp are not valid for "
                "the command provider"
            )
    elif provider in EXTERNAL_SECRET_PROVIDER_OPTIONS:
        if not password_ref:
            errors.append(
                "auth.login.password is required for external secret providers"
            )
        if password_command or otp_command:
            errors.append(
                "auth.login.password_command and auth.login.otp_command are "
                "only valid for the command provider"
            )
    elif password_ref or otp_ref or password_command or otp_command:
        errors.append(
            "auth.login password/OTP references require a password-manager or command provider"
        )
    return errors


def auth_source_errors(canonical: ConfigDict) -> list[str]:
    errors: list[str] = []
    token_store = canonical.get("auth.tokens.store") or DEFAULT_TOKEN_STORE
    provider = canonical.get("auth.login.provider") or DEFAULT_LOGIN_PROVIDER

    if token_store == "env-file" and not canonical.get("auth.tokens.env_file"):
        errors.append(
            "auth.tokens.env_file is required when auth.tokens.store is 'env-file'"
        )
    if provider == "env-file" and not canonical.get("auth.login.env_file"):
        errors.append(
            "auth.login.env_file is required when auth.login.provider is 'env-file'"
        )
    store_totp = canonical.get("auth.login.keyring_store_totp_secret")
    if store_totp and provider != "keyring":
        errors.append(
            "auth.login.keyring_store_totp_secret requires auth.login.provider = 'keyring'"
        )
    if store_totp and not canonical.get("auth.login.totp_serial"):
        errors.append(
            "auth.login.totp_serial is required when "
            "auth.login.keyring_store_totp_secret is enabled"
        )
    errors.extend(_login_reference_errors(canonical, provider))
    return errors


def unknown_config_key_error(key: str) -> str:
    if key in _GROUP_PATHS:
        return f"{key} must be a table of settings"
    legacy_hint = _LEGACY_KEY_HINTS.get(key)
    if legacy_hint:
        return f"{key!r} is a legacy config key; {legacy_hint}"
    suggestions = difflib.get_close_matches(key, _SUGGESTION_KEYS, n=1, cutoff=0.72)
    if suggestions:
        return f"unknown config key {key!r}. Did you mean {suggestions[0]!r}?"
    return f"unknown config key {key!r}"


def option_value_errors(opt: ConfigOption, value: Any) -> list[str]:
    key = opt.canonical_key
    if opt.normalize is bool:
        if isinstance(value, bool):
            return []
        return [f"{key} must be true or false, got {value!r}"]
    if opt.validate is not None:
        error = opt.validate(value)
        if error:
            return [f"{key} {error}"]
    if opt.falsey_uses_default and not value:
        return []
    if opt.choices and value not in opt.choices:
        return [f"{key} must be one of {format_choices(opt.choices)}, got {value!r}"]
    return []


def group_config_for_toml(raw: Mapping[str, Any]) -> ConfigDict:
    """Arrange a config into the grouped table layout used for TOML output."""
    canonical = canonicalize(raw)
    grouped: ConfigDict = {}
    for opt in CONFIG_OPTIONS:
        if opt.canonical_key not in canonical:
            continue
        table = grouped
        for group_part in opt.group.split("."):
            table = table.setdefault(group_part, {})
        table[opt.key] = canonical[opt.canonical_key]
    for key, value in canonical.items():
        if key not in _CANONICAL_KEYS:
            grouped[key] = value
    return grouped


# ---------------------------------------------------------------------------
# Legacy config support.
#
# Everything below exists to read legacy flat JSON configs (including their
# used_modules trees); it is applied to config.json files on load and by
# ``config migrate``, never to TOML configs. Delete this section together
# with JSON config support.
# ---------------------------------------------------------------------------

LEGACY_MODULES_KEY = "used_modules"
# Legacy spellings of the old "don't follow links" toggle; their boolean
# value is the inverse of links.follow_links.
LEGACY_NOLINKS_KEYS = ("no_links", "nolinks")
_LEGACY_QUIZ_ON_STRINGS = ("true", "yes")
_LEGACY_QUIZ_OFF_STRINGS = ("false", "no", "none")
FOLLOW_LINKS_KEY = "links.follow_links"

# Legacy flat JSON spelling -> canonical key.
LEGACY_KEY_MAP = {
    "user": "auth.user",
    "totp": "auth.login.totp_serial",
    "basedir": "paths.sync_directory",
    "cookie_file": "paths.cookie_file",
    "chromium_path": "paths.browser",
    "selected_courses": "courses.selected",
    "skip_courses": "courses.skip",
    "only_sync_semester": "courses.semesters",
    "course_prefix_handling": "courses.prefix_handling",
    "updatefiles": "downloads.update_files",
    "update_files": "downloads.update_files",
    "update_files_conflict": "downloads.conflict_handling",
    "exclude_filetypes": "filters.exclude_filetypes",
    "exclude_files": "filters.exclude_files",
    "exclude_links": "filters.exclude_links",
    "allowed_domains": "filters.allowed_domains",
    "exclude_sections": "filters.exclude_sections",
    "skip_sections": "filters.exclude_sections",
    "exclude_modules": "filters.exclude_modules",
    "skip_modules": "filters.exclude_modules",
}

# Where the entries of a legacy used_modules tree live now.
_LEGACY_MODULE_TREE_KEYS = {
    "assign": "modules.assignment",
    "resource": "modules.resource",
    "folder": "modules.folder",
}
_LEGACY_URL_TREE_KEYS = {
    "youtube": "links.youtube",
    "opencast": "links.opencast",
    "sciebo": "links.sciebo",
    "quiz": "modules.quiz",
}

# Hints shown when a legacy spelling appears where only the current format
# is accepted (i.e. outside the JSON conversion path).
_LEGACY_KEY_HINTS = {
    **{
        legacy: f"use {canonical!r} instead"
        for legacy, canonical in LEGACY_KEY_MAP.items()
    },
    **{
        legacy: f"use {FOLLOW_LINKS_KEY!r} (with the inverted value) instead"
        for legacy in LEGACY_NOLINKS_KEYS
    },
    LEGACY_MODULES_KEY: "use the [modules] and [links] tables instead",
    f"{LEGACY_MODULES_KEY}.url": "use the [modules] and [links] tables instead",
}


def convert_legacy_config(raw: Mapping[str, Any] | None) -> ConfigDict:
    """Translate a legacy flat JSON config into the current format.

    Resolves the legacy key spellings, inverts the old no_links/nolinks
    toggle into links.follow_links, maps used_modules trees onto the flat
    module/link keys (keeping their historical semantics: omitted entries
    stay disabled) and drops null values (omitting a key means "use the
    default"). Current-format keys and unknown keys pass through untouched.
    """
    converted: ConfigDict = {}
    for raw_key, value in _without_none(raw or {}).items():
        key = str(raw_key)
        if key in LEGACY_NOLINKS_KEYS:
            # Boolean values invert into follow_links; anything else is kept
            # as-is so validation reports the bad value.
            converted[FOLLOW_LINKS_KEY] = (
                (not value) if isinstance(value, bool) else value
            )
        elif key == LEGACY_MODULES_KEY and isinstance(value, Mapping):
            converted.update(_convert_legacy_used_modules(value))
        elif key in {"password", "totpsecret"}:
            # Released JSON secrets are consumed only by ``config migrate``;
            # they must never enter the current TOML configuration model.
            continue
        elif key == "use_secret_service":
            converted["auth.login.provider"] = (
                "keyring" if value else DEFAULT_LOGIN_PROVIDER
            )
        elif key == "secret_service_store_totp_secret":
            converted["auth.login.keyring_store_totp_secret"] = value
        elif key in LEGACY_KEY_MAP:
            converted[LEGACY_KEY_MAP[key]] = (
                "keep" if key == "update_files_conflict" and value == "none" else value
            )
        else:
            converted[key] = value
    return converted


def _without_none(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _without_none(value) if isinstance(value, Mapping) else value
        for key, value in mapping.items()
        if value is not None
    }


def _convert_legacy_used_modules(tree: Mapping[str, Any]) -> ConfigDict:
    if not tree:
        return {}
    flat: ConfigDict = {
        canonical: False
        for canonical in (
            *_LEGACY_MODULE_TREE_KEYS.values(),
            *_LEGACY_URL_TREE_KEYS.values(),
        )
    }
    flat[_LEGACY_URL_TREE_KEYS["quiz"]] = "off"
    for raw_key, value in tree.items():
        key = str(raw_key)
        if key == "url" and isinstance(value, Mapping):
            for url_key, url_value in value.items():
                canonical = _LEGACY_URL_TREE_KEYS.get(str(url_key))
                if canonical == _LEGACY_URL_TREE_KEYS["quiz"]:
                    url_value = _convert_legacy_quiz_value(url_value)
                flat[canonical or f"{LEGACY_MODULES_KEY}.url.{url_key}"] = url_value
        else:
            canonical = _LEGACY_MODULE_TREE_KEYS.get(key)
            flat[canonical or f"{LEGACY_MODULES_KEY}.{key}"] = value
    return flat


def _convert_legacy_quiz_value(value: Any) -> Any:
    """Map legacy quiz values (booleans, yes/no strings, mixed case) onto a mode string.

    Unrecognized values pass through so validation can report them.
    """
    if isinstance(value, bool):
        return "both" if value else "off"
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in QUIZ_MODES:
            return mode
        if mode in _LEGACY_QUIZ_ON_STRINGS:
            return "both"
        if mode in _LEGACY_QUIZ_OFF_STRINGS:
            return "off"
    return value
