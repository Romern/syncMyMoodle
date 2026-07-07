from __future__ import annotations

import copy
import difflib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, TypeAlias

from syncmymoodle.constants import COURSE_PREFIX_HANDLING_OPTIONS, QUIZ_MODES

PatternConfig: TypeAlias = dict[str, list[str]]
CliValueKind: TypeAlias = Literal["scalar", "csv", "flag"]

logger = logging.getLogger(__name__)
UPDATE_FILES_CONFLICT_OPTIONS = ("rename", "keep", "overwrite")
KEYRING_CONFIG_KEYS = ("use_secret_service", "secret_service_store_totp_secret")
CONFIG_GROUPS = {
    "auth": (
        "user",
        "password",
        "totp",
        "totpsecret",
        "use_secret_service",
        "secret_service_store_totp_secret",
    ),
    "paths": ("basedir", "cookie_file", "chromium_path"),
    "courses": (
        "selected_courses",
        "skip_courses",
        "only_sync_semester",
        "course_prefix_handling",
    ),
    "downloads": (
        "exclude_filetypes",
        "exclude_files",
        "update_files",
        "updatefiles",
        "update_files_conflict",
    ),
    "links": ("no_links", "nolinks", "exclude_links", "allowed_domains"),
    "skip_rules": (
        "exclude_sections",
        "skip_sections",
        "exclude_modules",
        "skip_modules",
    ),
}
MODULES_CONFIG_GROUP = "modules"
TOP_LEVEL_MODULE_KEYS = ("assign", "resource", "url", "folder")
URL_MODULE_KEYS = ("youtube", "opencast", "sciebo", "quiz")
BOOLEAN_MODULE_KEYS = ("assign", "resource", "folder")
BOOLEAN_URL_MODULE_KEYS = ("youtube", "opencast", "sciebo")
LEGACY_QUIZ_MODE_STRINGS = ("true", "yes", "false", "no", "none")

# Default module toggle tree used when the config file does not define one.
DEFAULT_USED_MODULES: dict[str, Any] = {
    "assign": True,
    "resource": True,
    "url": {"youtube": True, "opencast": True, "sciebo": True, "quiz": "html"},
    "folder": True,
}


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


def normalize_quiz_mode(value: Any, log: logging.Logger = logger) -> str:
    """Map a configured quiz value onto one of :data:`QUIZ_MODES`.

    Accepts the legacy booleans (``True`` -> ``"both"``, ``False``/absent ->
    ``"off"``) as well as the mode strings ``"off"``/``"html"``/``"pdf"``/
    ``"both"``. Unrecognized values are treated as ``"off"`` with a warning.
    """
    if value is True:
        return "both"
    if value is False or value is None:
        return "off"
    mode = str(value).strip().lower()
    if mode in QUIZ_MODES:
        return mode
    if mode in ("none", "false", "no"):
        return "off"
    if mode in ("true", "yes"):
        return "both"
    log.warning(
        "Unrecognized quiz mode %r; expected one of %s. Disabling quizzes.",
        value,
        ", ".join(QUIZ_MODES),
    )
    return "off"


def normalize_used_modules(value: Any) -> dict[str, Any]:
    used_modules = copy.deepcopy(value or DEFAULT_USED_MODULES)
    if isinstance(used_modules.get("url"), dict):
        used_modules["url"]["quiz"] = normalize_quiz_mode(
            used_modules["url"].get("quiz")
        )
    return used_modules


def identity(value: Any) -> Any:
    return value


@dataclass(frozen=True)
class CliOverride:
    arg_name: str
    value_kind: CliValueKind


@dataclass(frozen=True)
class ConfigOption:
    field_name: str
    config_keys: tuple[str, ...]
    default: Any = None
    normalize: Callable[[Any], Any] = identity
    falsey_uses_default: bool = False
    cli: CliOverride | None = None
    choices: tuple[str, ...] = ()

    @property
    def canonical_key(self) -> str:
        return self.config_keys[0]

    def value_from(self, raw: Mapping[str, Any]) -> Any:
        value: Any = _MISSING
        for key in self.config_keys:
            if key in raw:
                value = raw[key]
                break

        if value is _MISSING or (self.falsey_uses_default and not value):
            value = self.default

        return self.normalize(copy.deepcopy(value))


_MISSING = object()

CONFIG_OPTIONS = (
    ConfigOption(
        "user",
        ("user",),
        cli=CliOverride("user", "scalar"),
    ),
    ConfigOption(
        "password",
        ("password",),
        cli=CliOverride("password", "scalar"),
    ),
    ConfigOption(
        "totp",
        ("totp",),
        cli=CliOverride("totp", "scalar"),
    ),
    ConfigOption(
        "totpsecret",
        ("totpsecret",),
        cli=CliOverride("totpsecret", "scalar"),
    ),
    ConfigOption(
        "cookie_file",
        ("cookie_file",),
        "./session",
        falsey_uses_default=True,
        cli=CliOverride("cookiefile", "scalar"),
    ),
    ConfigOption(
        "basedir",
        ("basedir",),
        "./",
        falsey_uses_default=True,
        cli=CliOverride("basedir", "scalar"),
    ),
    ConfigOption(
        "course_prefix_handling",
        ("course_prefix_handling",),
        "keep",
        falsey_uses_default=True,
        cli=CliOverride("courseprefix", "scalar"),
        choices=COURSE_PREFIX_HANDLING_OPTIONS,
    ),
    ConfigOption(
        "chromium_path",
        ("chromium_path",),
        None,
        falsey_uses_default=True,
        cli=CliOverride("chromiumpath", "scalar"),
    ),
    ConfigOption(
        "nolinks",
        ("nolinks", "no_links"),
        False,
        bool,
        cli=CliOverride("nolinks", "flag"),
    ),
    ConfigOption(
        "updatefiles",
        ("updatefiles", "update_files"),
        False,
        bool,
        cli=CliOverride("updatefiles", "flag"),
    ),
    ConfigOption(
        "update_files_conflict",
        ("update_files_conflict",),
        "rename",
        falsey_uses_default=True,
        cli=CliOverride("updatefilesconflict", "scalar"),
        choices=UPDATE_FILES_CONFLICT_OPTIONS,
    ),
    ConfigOption(
        "selected_courses",
        ("selected_courses",),
        normalize=as_string_list,
        cli=CliOverride("courses", "csv"),
    ),
    ConfigOption(
        "skip_courses",
        ("skip_courses",),
        normalize=as_string_list,
        cli=CliOverride("skipcourses", "csv"),
    ),
    ConfigOption(
        "only_sync_semester",
        ("only_sync_semester",),
        normalize=as_string_list,
        cli=CliOverride("semester", "csv"),
    ),
    ConfigOption(
        "exclude_filetypes",
        ("exclude_filetypes",),
        normalize=as_string_list,
        cli=CliOverride("excludefiletypes", "csv"),
    ),
    ConfigOption(
        "exclude_files",
        ("exclude_files",),
        normalize=as_string_list,
        cli=CliOverride("excludefiles", "csv"),
    ),
    ConfigOption(
        "exclude_links",
        ("exclude_links",),
        normalize=normalize_pattern_config,
        cli=CliOverride("excludelinks", "csv"),
    ),
    ConfigOption(
        "allowed_domains",
        ("allowed_domains",),
        normalize=normalize_pattern_config,
        cli=CliOverride("alloweddomains", "csv"),
    ),
    ConfigOption(
        "exclude_sections",
        ("exclude_sections", "skip_sections"),
        normalize=normalize_pattern_config,
        cli=CliOverride("excludesections", "csv"),
    ),
    ConfigOption(
        "exclude_modules",
        ("exclude_modules", "skip_modules"),
        normalize=normalize_pattern_config,
        cli=CliOverride("excludemodules", "csv"),
    ),
    ConfigOption(
        "used_modules",
        ("used_modules",),
        DEFAULT_USED_MODULES,
        normalize_used_modules,
        falsey_uses_default=True,
    ),
)
CONFIG_OPTIONS_BY_FIELD = {option.field_name: option for option in CONFIG_OPTIONS}
KNOWN_CONFIG_KEYS = frozenset(
    key for option in CONFIG_OPTIONS for key in option.config_keys
) | frozenset(KEYRING_CONFIG_KEYS)
GROUPED_CONFIG_KEYS = frozenset(key for keys in CONFIG_GROUPS.values() for key in keys)
KNOWN_DISPLAY_CONFIG_KEYS = frozenset(KNOWN_CONFIG_KEYS) | frozenset(
    list(CONFIG_GROUPS)
    + [MODULES_CONFIG_GROUP]
    + [
        f"{group_name}.{key}"
        for group_name, keys in CONFIG_GROUPS.items()
        for key in keys
    ]
    + [f"{MODULES_CONFIG_GROUP}.{key}" for key in TOP_LEVEL_MODULE_KEYS if key != "url"]
    + [f"{MODULES_CONFIG_GROUP}.url.{key}" for key in URL_MODULE_KEYS]
)


class ConfigValidationError(ValueError):
    pass


def validate_config(raw: Mapping[str, Any]) -> None:
    errors = config_validation_errors(expand_config_groups(raw))
    if errors:
        raise ConfigValidationError(
            "invalid config:\n" + "\n".join(f"- {error}" for error in errors)
        )


def expand_config_groups(raw: Mapping[str, Any]) -> dict[str, Any]:
    expanded: dict[str, Any] = {}
    for key, value in raw.items():
        if key in CONFIG_GROUPS:
            if isinstance(value, Mapping):
                for child_key, child_value in value.items():
                    if child_key in CONFIG_GROUPS[key]:
                        expanded[str(child_key)] = child_value
                    else:
                        expanded[f"{key}.{child_key}"] = child_value
            else:
                expanded[key] = value
        elif key == MODULES_CONFIG_GROUP:
            expanded["used_modules"] = value
        else:
            expanded[key] = value
    return expanded


def group_config_for_toml(raw: Mapping[str, Any]) -> dict[str, Any]:
    flat = expand_config_groups(raw)
    grouped: dict[str, Any] = {}

    for group_name, keys in CONFIG_GROUPS.items():
        group_values = {
            key: flat[key] for key in keys if key in flat and key in GROUPED_CONFIG_KEYS
        }
        if group_values:
            grouped[group_name] = group_values

    if "used_modules" in flat:
        grouped[MODULES_CONFIG_GROUP] = flat["used_modules"]

    grouped_keys = set().union(*CONFIG_GROUPS.values())
    for key, value in flat.items():
        if key not in grouped_keys and key != "used_modules":
            grouped[key] = value
    return grouped


def config_validation_errors(raw: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    unknown_keys = sorted(set(raw) - KNOWN_CONFIG_KEYS)
    errors.extend(unknown_config_key_error(key) for key in unknown_keys)

    errors.extend(choice_validation_errors(raw))
    errors.extend(boolean_validation_errors(raw))
    errors.extend(used_modules_validation_errors(raw.get("used_modules")))
    return errors


def choice_validation_errors(raw: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for option in CONFIG_OPTIONS:
        if not option.choices:
            continue
        for key in option.config_keys:
            if key not in raw:
                continue
            value = raw[key]
            if option.falsey_uses_default and not value:
                break
            if value not in option.choices:
                errors.append(
                    f"{key} must be one of {format_choices(option.choices)}, "
                    f"got {value!r}"
                )
            break
    return errors


def unknown_config_key_error(key: str) -> str:
    suggestions = difflib.get_close_matches(
        key,
        KNOWN_DISPLAY_CONFIG_KEYS,
        n=1,
        cutoff=0.72,
    )
    if suggestions:
        return f"unknown config key {key!r}. Did you mean {suggestions[0]!r}?"
    return f"unknown config key {key!r}"


def boolean_validation_errors(raw: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for option in CONFIG_OPTIONS:
        if option.normalize is bool:
            for key in option.config_keys:
                if key in raw:
                    errors.extend(validate_boolean_value(key, raw[key]))
    for key in KEYRING_CONFIG_KEYS:
        if key in raw:
            errors.extend(validate_boolean_value(key, raw[key]))
    return errors


def validate_boolean_value(key: str, value: Any) -> list[str]:
    if isinstance(value, bool):
        return []
    return [f"{key} must be true or false, got {value!r}"]


def used_modules_validation_errors(value: Any) -> list[str]:
    if not value:
        return []
    if not isinstance(value, Mapping):
        return [f"used_modules must be a table/object, got {type(value).__name__}"]

    errors: list[str] = []
    unknown_modules = sorted(set(value) - set(TOP_LEVEL_MODULE_KEYS))
    if unknown_modules:
        errors.append(
            f"used_modules contains unknown key(s): {', '.join(unknown_modules)}"
        )

    for key in BOOLEAN_MODULE_KEYS:
        if key in value:
            errors.extend(validate_boolean_value(f"used_modules.{key}", value[key]))

    if "url" in value:
        errors.extend(url_modules_validation_errors(value["url"]))
    return errors


def url_modules_validation_errors(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"used_modules.url must be a table/object, got {type(value).__name__}"]

    errors: list[str] = []
    unknown_modules = sorted(set(value) - set(URL_MODULE_KEYS))
    if unknown_modules:
        errors.append(
            f"used_modules.url contains unknown key(s): {', '.join(unknown_modules)}"
        )

    for key in BOOLEAN_URL_MODULE_KEYS:
        if key in value:
            errors.extend(validate_boolean_value(f"used_modules.url.{key}", value[key]))

    if "quiz" in value:
        errors.extend(validate_quiz_value(value["quiz"]))
    return errors


def validate_quiz_value(value: Any) -> list[str]:
    if isinstance(value, bool):
        return []
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in QUIZ_MODES or mode in LEGACY_QUIZ_MODE_STRINGS:
            return []
    return [
        "used_modules.url.quiz must be one of "
        f"{format_choices(QUIZ_MODES)} or a legacy boolean, got {value!r}"
    ]


def format_choices(choices: tuple[str, ...]) -> str:
    return ", ".join(repr(choice) for choice in choices)


@dataclass
class Config:
    """Typed view of the user configuration.

    ``from_dict`` is the single place where defaults are applied and legacy
    key aliases are resolved, so the rest of the code can read plain typed
    attributes instead of ``config.get(key, default)``.
    """

    # Credentials / login
    user: str | None = None
    password: str | None = None
    totp: str | None = None
    totpsecret: str | None = None
    cookie_file: str = "./session"

    # Local sync target and naming
    basedir: str = "./"
    course_prefix_handling: str = "keep"

    # Explicit path to a Chromium-family browser used to render quiz PDFs. When
    # unset, the browser is auto-discovered (see downloader.find_chromium).
    chromium_path: str | None = None

    # Link/download behaviour
    nolinks: bool = False
    updatefiles: bool = False
    update_files_conflict: str = "rename"

    # Course/semester selection
    selected_courses: list[str] = field(default_factory=list)
    skip_courses: list[str] = field(default_factory=list)
    only_sync_semester: list[str] = field(default_factory=list)

    exclude_filetypes: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)
    exclude_links: PatternConfig = field(default_factory=dict)
    allowed_domains: PatternConfig = field(default_factory=dict)
    exclude_sections: PatternConfig = field(default_factory=dict)
    exclude_modules: PatternConfig = field(default_factory=dict)

    # Module toggle tree (see DEFAULT_USED_MODULES).
    used_modules: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "Config":
        raw = expand_config_groups(raw or {})
        return cls(
            **{option.field_name: option.value_from(raw) for option in CONFIG_OPTIONS}
        )

    def module_enabled(self, name: str) -> bool:
        """Whether a top-level module type is enabled (assign/resource/folder/url)."""
        return bool(self.used_modules.get(name))

    def url_module_enabled(self, name: str) -> bool:
        """Whether a url sub-module is enabled (youtube/opencast/sciebo/quiz)."""
        url = self.used_modules.get("url")
        if not isinstance(url, dict):
            return False
        if name == "quiz":
            # quiz is a mode string ("off"/"html"/"pdf"/"both"), not a bool.
            return normalize_quiz_mode(url.get("quiz")) != "off"
        return bool(url.get(name))

    @property
    def quiz_mode(self) -> str:
        """Quiz output mode: one of off/html/pdf/both (see QUIZ_MODES)."""
        url = self.used_modules.get("url")
        value = url.get("quiz") if isinstance(url, dict) else None
        return normalize_quiz_mode(value)
