from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, TypeAlias

from syncmymoodle.constants import COURSE_PREFIX_HANDLING_OPTIONS, QUIZ_MODES

PatternConfig: TypeAlias = dict[str, list[str]]
CliValueKind: TypeAlias = Literal["scalar", "csv", "flag"]

logger = logging.getLogger(__name__)
UPDATE_FILES_CONFLICT_OPTIONS = ("rename", "keep", "overwrite")

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
    ),
    ConfigOption(
        "exclude_links",
        ("exclude_links",),
        normalize=normalize_pattern_config,
    ),
    ConfigOption(
        "allowed_domains",
        ("allowed_domains",),
        normalize=normalize_pattern_config,
    ),
    ConfigOption(
        "exclude_sections",
        ("exclude_sections", "skip_sections"),
        normalize=normalize_pattern_config,
    ),
    ConfigOption(
        "exclude_modules",
        ("exclude_modules", "skip_modules"),
        normalize=normalize_pattern_config,
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
        raw = dict(raw or {})
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
