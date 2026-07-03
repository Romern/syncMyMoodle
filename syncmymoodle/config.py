from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping

# Default module toggle tree used when the config file does not define one.
DEFAULT_USED_MODULES: dict[str, Any] = {
    "assign": True,
    "resource": True,
    "url": {"youtube": True, "opencast": True, "sciebo": True, "quiz": False},
    "folder": True,
}


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
    selected_courses: list[Any] = field(default_factory=list)
    skip_courses: list[Any] = field(default_factory=list)
    only_sync_semester: list[Any] = field(default_factory=list)

    # Exclusions. exclude_links/sections/modules and allowed_domains may be
    # either a flat list or a per-course dict ({"*": [...], "<id>": [...]}).
    exclude_filetypes: list[Any] = field(default_factory=list)
    exclude_files: list[Any] = field(default_factory=list)
    exclude_links: Any = field(default_factory=list)
    allowed_domains: Any = field(default_factory=list)
    exclude_sections: Any = field(default_factory=list)
    exclude_modules: Any = field(default_factory=list)

    # Module toggle tree (see DEFAULT_USED_MODULES).
    used_modules: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "Config":
        raw = dict(raw or {})

        used_modules = raw.get("used_modules") or copy.deepcopy(DEFAULT_USED_MODULES)
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
            selected_courses=raw.get("selected_courses") or [],
            skip_courses=raw.get("skip_courses") or [],
            only_sync_semester=raw.get("only_sync_semester") or [],
            exclude_filetypes=raw.get("exclude_filetypes") or [],
            exclude_files=raw.get("exclude_files") or [],
            exclude_links=raw.get("exclude_links") or [],
            allowed_domains=raw.get("allowed_domains") or [],
            exclude_sections=raw.get("exclude_sections", raw.get("skip_sections", []))
            or [],
            exclude_modules=raw.get("exclude_modules", raw.get("skip_modules", []))
            or [],
            used_modules=used_modules,
        )

    def module_enabled(self, name: str) -> bool:
        """Whether a top-level module type is enabled (assign/resource/folder/url)."""
        return bool(self.used_modules.get(name))

    def url_module_enabled(self, name: str) -> bool:
        """Whether a url sub-module is enabled (youtube/opencast/sciebo/quiz)."""
        url = self.used_modules.get("url")
        return bool(url.get(name)) if isinstance(url, dict) else False
