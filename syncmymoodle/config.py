from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias

PatternConfig: TypeAlias = dict[str, list[str]]

# Default module toggle tree used when the config file does not define one.
DEFAULT_USED_MODULES: dict[str, Any] = {
    "assign": True,
    "resource": True,
    "url": {"youtube": True, "opencast": True, "sciebo": True, "quiz": False},
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

        used_modules = copy.deepcopy(raw.get("used_modules") or DEFAULT_USED_MODULES)
        # Quiz PDF generation is disabled until the pdfkit/wkhtmltopdf renderer
        # is replaced with a safer implementation. Enforce it regardless of the
        # configured value so no entry point can accidentally re-enable it.
        if isinstance(used_modules.get("url"), dict):
            used_modules["url"]["quiz"] = False

        return cls(
            user=raw.get("user"),
            password=raw.get("password"),
            totp=raw.get("totp"),
            totpsecret=raw.get("totpsecret"),
            cookie_file=raw.get("cookie_file") or "./session",
            basedir=raw.get("basedir") or "./",
            course_prefix_handling=raw.get("course_prefix_handling") or "keep",
            nolinks=bool(raw.get("nolinks", raw.get("no_links", False))),
            updatefiles=bool(raw.get("updatefiles", raw.get("update_files", False))),
            update_files_conflict=raw.get("update_files_conflict") or "rename",
            selected_courses=as_string_list(raw.get("selected_courses")),
            skip_courses=as_string_list(raw.get("skip_courses")),
            only_sync_semester=as_string_list(raw.get("only_sync_semester")),
            exclude_filetypes=as_string_list(raw.get("exclude_filetypes")),
            exclude_files=as_string_list(raw.get("exclude_files")),
            exclude_links=normalize_pattern_config(raw.get("exclude_links")),
            allowed_domains=normalize_pattern_config(raw.get("allowed_domains")),
            exclude_sections=normalize_pattern_config(
                raw.get("exclude_sections", raw.get("skip_sections"))
            ),
            exclude_modules=normalize_pattern_config(
                raw.get("exclude_modules", raw.get("skip_modules"))
            ),
            used_modules=used_modules,
        )

    def module_enabled(self, name: str) -> bool:
        """Whether a top-level module type is enabled (assign/resource/folder/url)."""
        return bool(self.used_modules.get(name))

    def url_module_enabled(self, name: str) -> bool:
        """Whether a url sub-module is enabled (youtube/opencast/sciebo/quiz)."""
        url = self.used_modules.get("url")
        return bool(url.get(name)) if isinstance(url, dict) else False
