"""Configuration schema, normalization and validation.

The :class:`Config` dataclass is the single source of truth for every
option: each field carries its TOML group, CLI override, normalizer and
validation rules via :func:`option`. The CLI parser, config validation,
the canonical flat form and the TOML layout written by ``config migrate``
are all derived from this schema.

:func:`canonicalize` maps a config in the current format (grouped tables
or flat ``"group.key"`` names) onto one flat canonical dict before any
merging happens. Legacy JSON configs are translated into the current
format first by :func:`convert_legacy_config` (see the legacy support
section at the bottom of this module); legacy spellings are not accepted
in TOML configs, but validation points from them to the current names.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Literal, TypeAlias, cast

from syncmymoodle.constants import COURSE_PREFIX_HANDLING_OPTIONS, QUIZ_MODES

PatternConfig: TypeAlias = dict[str, list[str]]
ConfigDict: TypeAlias = dict[str, Any]
CliValueKind: TypeAlias = Literal["scalar", "csv", "flag"]

UPDATE_FILES_CONFLICT_OPTIONS = ("rename", "keep", "overwrite")


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


def normalize_pattern_config(value: Any) -> PatternConfig:
    if isinstance(value, Mapping):
        return {
            str(key): patterns
            for key, raw_patterns in value.items()
            if (patterns := as_string_list(raw_patterns))
        }
    patterns = as_string_list(value)
    return {"*": patterns} if patterns else {}


def format_choices(choices: tuple[str, ...]) -> str:
    return ", ".join(repr(choice) for choice in choices)


@dataclass(frozen=True)
class CliOverride:
    arg_name: str
    value_kind: CliValueKind
    help: str
    # Value a "flag" kind writes when given (--nolinks writes False into
    # links.follow_links).
    flag_value: bool = True
    requires_keyring: bool = False


def cli_arg(arg_name: str, help_text: str) -> CliOverride:
    return CliOverride(arg_name, "scalar", help_text)


def cli_csv(arg_name: str, help_text: str) -> CliOverride:
    return CliOverride(arg_name, "csv", help_text)


def cli_flag(
    arg_name: str,
    help_text: str,
    flag_value: bool = True,
    requires_keyring: bool = False,
) -> CliOverride:
    return CliOverride(arg_name, "flag", help_text, flag_value, requires_keyring)


def option(
    default: Any = None,
    *,
    group: str,
    key: str | None = None,
    factory: Callable[[], Any] | None = None,
    normalize: Callable[[Any], Any] = identity,
    falsey_uses_default: bool = False,
    choices: tuple[str, ...] = (),
    cli: CliOverride | None = None,
) -> Any:
    """Declare a :class:`Config` field together with its option schema.

    ``key`` is the spelling used inside the option's ``group`` table
    (defaults to the field name); the flat canonical spelling is
    ``"group.key"``.
    """
    metadata = {
        "config": {
            "group": group,
            "key": key,
            "normalize": normalize,
            "falsey_uses_default": falsey_uses_default,
            "choices": choices,
            "cli": cli,
        }
    }
    if factory is not None:
        return field(default_factory=factory, metadata=metadata)
    return field(default=default, metadata=metadata)


@dataclass
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
        cli=cli_arg("user", "set your RWTH Single Sign-On username"),
    )
    password: str | None = option(
        group="auth",
        cli=cli_arg("password", "set your RWTH Single Sign-On password"),
    )
    totp: str | None = option(
        group="auth",
        cli=cli_arg(
            "totp",
            "set your RWTH Single Sign-On TOTP provider's serial number "
            "(see https://idm.rwth-aachen.de/selfservice/MFATokenManager)",
        ),
    )
    totpsecret: str | None = option(
        group="auth",
        cli=cli_arg(
            "totpsecret", "(optional) set your RWTH Single Sign-On TOTP provider Secret"
        ),
    )
    use_secret_service: bool = option(
        False,
        group="auth",
        normalize=bool,
        cli=cli_flag(
            "secretservice",
            "Use system's keyring for storing and retrieving account credentials",
            requires_keyring=True,
        ),
    )
    secret_service_store_totp_secret: bool = option(
        False,
        group="auth",
        normalize=bool,
        cli=cli_flag(
            "secretservicetotpsecret",
            "Save TOTP secret in keyring",
            requires_keyring=True,
        ),
    )

    # Local paths
    basedir: str = option(
        "./",
        group="paths",
        falsey_uses_default=True,
        cli=cli_arg("basedir", "specify the directory where all files will be synced"),
    )
    cookie_file: str = option(
        "./session",
        group="paths",
        falsey_uses_default=True,
        cli=cli_arg("cookiefile", "set the location of a cookie file"),
    )
    # Explicit path to a Chromium-family browser used to render quiz PDFs. When
    # unset, the browser is auto-discovered (see quiz.find_chromium).
    chromium_path: str | None = option(
        group="paths",
        falsey_uses_default=True,
        cli=cli_arg(
            "chromiumpath",
            "set the path to a Chrome/Chromium/Edge binary for quiz PDF rendering",
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
            "specify the courses that should be synced using comma-separated "
            "links. Defaults to all courses, if no additional restrictions "
            "e.g. semester are defined.",
        ),
    )
    skip_courses: list[str] = option(
        group="courses",
        key="skip",
        factory=list,
        normalize=as_string_list,
        cli=cli_csv(
            "skipcourses",
            "exclude specific courses using comma-separated links. Defaults to None.",
        ),
    )
    only_sync_semester: list[str] = option(
        group="courses",
        key="semesters",
        factory=list,
        normalize=as_string_list,
        cli=cli_csv(
            "semester",
            "specify semesters to be synced e.g. `22s`, comma-separated. "
            "Defaults to all semesters, if no additional restrictions e.g. "
            "courses are defined.",
        ),
    )
    course_prefix_handling: str = option(
        "keep",
        group="courses",
        key="prefix_handling",
        falsey_uses_default=True,
        choices=COURSE_PREFIX_HANDLING_OPTIONS,
        cli=cli_arg(
            "courseprefix",
            "handle leading two-character course prefixes in local folder "
            "names: 'keep' (default), 'remove', or 'suffix'",
        ),
    )

    # Download behaviour
    update_files: bool = option(
        False,
        group="downloads",
        normalize=bool,
        cli=cli_flag(
            "updatefiles",
            "define whether modified files with the same name/path should be "
            "redownloaded",
        ),
    )
    update_files_conflict: str = option(
        "rename",
        group="downloads",
        falsey_uses_default=True,
        choices=UPDATE_FILES_CONFLICT_OPTIONS,
        cli=cli_arg(
            "updatefilesconflict",
            "define how to handle locally modified files when updating: "
            "'rename' (default) moves the old file aside, 'keep' skips the "
            "update, 'overwrite' replaces the local file",
        ),
    )

    # Exclude/allow rules
    exclude_filetypes: list[str] = option(
        group="filters",
        factory=list,
        normalize=as_string_list,
        cli=cli_csv(
            "excludefiletypes",
            "specify whether specific file types should be excluded, "
            'comma-separated e.g. "mp4,mkv"',
        ),
    )
    exclude_files: list[str] = option(
        group="filters",
        factory=list,
        normalize=as_string_list,
        cli=cli_csv(
            "excludefiles",
            'exclude specific files using comma-separated patterns e.g. "*.bak,*.tmp"',
        ),
    )
    exclude_links: PatternConfig = option(
        group="filters",
        factory=dict,
        normalize=normalize_pattern_config,
        cli=cli_csv(
            "excludelinks",
            "exclude discovered links using comma-separated URL patterns",
        ),
    )
    allowed_domains: PatternConfig = option(
        group="filters",
        factory=dict,
        normalize=normalize_pattern_config,
        cli=cli_csv(
            "alloweddomains",
            "only keep discovered links on these comma-separated domains",
        ),
    )
    exclude_sections: PatternConfig = option(
        group="filters",
        factory=dict,
        normalize=normalize_pattern_config,
        cli=cli_csv(
            "excludesections",
            "exclude Moodle sections by comma-separated names, ids or patterns",
        ),
    )
    exclude_modules: PatternConfig = option(
        group="filters",
        factory=dict,
        normalize=normalize_pattern_config,
        cli=cli_csv(
            "excludemodules",
            "exclude Moodle modules by comma-separated names, ids, types, "
            "URLs or patterns",
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
            "nolinks",
            "define whether various links in moodle pages should also be "
            "inspected e.g. youtube videos, wikipedia articles",
            flag_value=False,
        ),
    )
    link_youtube: bool = option(True, group="links", key="youtube", normalize=bool)
    link_opencast: bool = option(True, group="links", key="opencast", normalize=bool)
    link_sciebo: bool = option(True, group="links", key="sciebo", normalize=bool)

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
        cli=cli_arg(
            "quiz", "save quiz review attempts as 'off', 'html', 'pdf', or 'both'"
        ),
    )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "Config":
        canonical = canonicalize(raw)
        kwargs: dict[str, Any] = {}
        for opt in CONFIG_OPTIONS:
            if opt.canonical_key not in canonical:
                continue
            value = canonical[opt.canonical_key]
            if opt.falsey_uses_default and not value:
                continue
            kwargs[opt.field_name] = opt.normalize(value)
        return cls(**kwargs)

    def module_enabled(self, name: str) -> bool:
        """Whether a Moodle activity type is enabled (assignment/resource/folder)."""
        flags = {
            "assignment": self.module_assignment,
            "resource": self.module_resource,
            "folder": self.module_folder,
        }
        return flags.get(name, False)

    def link_source_enabled(self, name: str) -> bool:
        """Whether a link-based content source is enabled (youtube/opencast/sciebo).

        All sources require link inspection (follow_links) to be on.
        """
        flags = {
            "youtube": self.link_youtube,
            "opencast": self.link_opencast,
            "sciebo": self.link_sciebo,
        }
        return self.follow_links and flags.get(name, False)


@dataclass(frozen=True)
class ConfigOption:
    field_name: str
    group: str
    key: str
    canonical_key: str
    normalize: Callable[[Any], Any]
    falsey_uses_default: bool
    choices: tuple[str, ...]
    cli: CliOverride | None


def _build_config_options() -> tuple[ConfigOption, ...]:
    options = []
    for config_field in fields(Config):
        meta = cast(dict[str, Any], config_field.metadata["config"])
        group = cast(str, meta["group"])
        key = cast(str, meta["key"] or config_field.name)
        options.append(
            ConfigOption(
                field_name=config_field.name,
                group=group,
                key=key,
                canonical_key=f"{group}.{key}",
                normalize=meta["normalize"],
                falsey_uses_default=meta["falsey_uses_default"],
                choices=meta["choices"],
                cli=meta["cli"],
            )
        )
    return tuple(options)


CONFIG_OPTIONS = _build_config_options()
_CANONICAL_KEYS = frozenset(opt.canonical_key for opt in CONFIG_OPTIONS)
_GROUP_PATHS = frozenset(opt.group for opt in CONFIG_OPTIONS)
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


def validate_config(raw: Mapping[str, Any]) -> None:
    errors = config_validation_errors(raw)
    if errors:
        raise ConfigValidationError(None, errors)


def config_validation_errors(raw: Mapping[str, Any]) -> list[str]:
    canonical = canonicalize(raw)
    errors = [
        unknown_config_key_error(key)
        for key in sorted(set(canonical) - _CANONICAL_KEYS)
    ]
    for opt in CONFIG_OPTIONS:
        if opt.canonical_key in canonical:
            errors.extend(option_value_errors(opt, canonical[opt.canonical_key]))
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
    if opt.choices and not (opt.falsey_uses_default and not value):
        if value not in opt.choices:
            return [
                f"{key} must be one of {format_choices(opt.choices)}, got {value!r}"
            ]
    return []


def group_config_for_toml(raw: Mapping[str, Any]) -> ConfigDict:
    """Arrange a config into the grouped table layout used for TOML output."""
    canonical = canonicalize(raw)
    grouped: ConfigDict = {}
    for opt in CONFIG_OPTIONS:
        if opt.canonical_key not in canonical:
            continue
        grouped.setdefault(opt.group, {})[opt.key] = canonical[opt.canonical_key]
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
    "password": "auth.password",
    "totp": "auth.totp",
    "totpsecret": "auth.totpsecret",
    "use_secret_service": "auth.use_secret_service",
    "secret_service_store_totp_secret": "auth.secret_service_store_totp_secret",
    "basedir": "paths.basedir",
    "cookie_file": "paths.cookie_file",
    "chromium_path": "paths.chromium_path",
    "selected_courses": "courses.selected",
    "skip_courses": "courses.skip",
    "only_sync_semester": "courses.semesters",
    "course_prefix_handling": "courses.prefix_handling",
    "updatefiles": "downloads.update_files",
    "update_files": "downloads.update_files",
    "update_files_conflict": "downloads.update_files_conflict",
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
        elif key in LEGACY_KEY_MAP:
            converted[LEGACY_KEY_MAP[key]] = value
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
    """Map legacy quiz values (booleans, yes/no strings, mixed case) onto a
    mode string.

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
