from __future__ import annotations

import hashlib
import html
import ntpath
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from syncmymoodle.constants import INVALID_CHARS

if TYPE_CHECKING:
    from syncmymoodle.node import Node

WINDOWS_EXTENDED_PATH_THRESHOLD = 240
# Leave room for download sidecars and ``.syncconflict`` suffixes while staying
# below the 255-byte component limit used by common Linux and macOS filesystems.
# A UTF-8 byte limit is also conservative for NTFS's 255 UTF-16-code-unit limit.
PATH_COMPONENT_MAX_BYTES = 220
PATH_COMPONENT_HASH_LENGTH = 8


def is_windows() -> bool:
    return os.name == "nt"


def absolute_path(path: Path, base_dir: Path | None = None) -> Path:
    """Return an absolute path without depending on later CWD changes."""
    path = path.expanduser()
    if path.is_absolute():
        return Path(os.path.abspath(path))
    if base_dir is None:
        base_dir = Path.cwd()
    return Path(os.path.abspath(absolute_path(base_dir) / path))


def path_identity(value: object) -> tuple[bool, str] | None:
    """Return a normalized identity for detecting aliased managed paths."""
    if not isinstance(value, (str, os.PathLike)) or not value:
        return None
    path = Path(value).expanduser()
    is_absolute = path.is_absolute()
    normalized = os.path.realpath(path) if is_absolute else os.path.normpath(path)
    return is_absolute, os.path.normcase(os.fspath(normalized))


def user_config_dir() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        root = Path(xdg_config_home).expanduser()
    elif is_windows():
        appdata = os.environ.get("APPDATA")
        if appdata:
            root = Path(appdata).expanduser()
        else:
            root = Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path("~/.config").expanduser()
    return absolute_path(root / "syncmymoodle")


def is_windows_reserved_path_part(path: str) -> bool:
    isreserved = getattr(ntpath, "isreserved", None)
    if isreserved is not None:
        return bool(isreserved(path))
    return PureWindowsPath(path).is_reserved()


CONFLICT_MARKER = "syncconflict"
CONFLICT_GLOB = f"*.{CONFLICT_MARKER}.*"
CONFLICT_RE = re.compile(
    rf"^(?P<stem>.+)\.{CONFLICT_MARKER}\."
    r"(?P<tag>[0-9a-f]{8}|unknown|missing)"
    r"\.copy\.(?P<index>\d+)"
    r"(?P<suffix>.*)$"
)
LEGACY_CONFLICT_RE = re.compile(
    rf"^(?P<stem>.+)\.{CONFLICT_MARKER}\."
    r"(?P<tag>[0-9a-f]{8}|unknown|missing)"
    r"(?:\.(?P<index>\d+))?"
    r"(?P<suffix>.*)$"
)


@dataclass(frozen=True)
class ConflictPathInfo:
    canonical: Path
    index: int


def sanitize_path_part(path: str) -> str:
    path = urllib.parse.unquote(path)
    while (unescaped := html.unescape(path)) != path:
        path = unescaped
    path = "".join(
        character
        for character in path
        if character not in INVALID_CHARS and ord(character) >= 32
    )
    path = path.lstrip(" ").rstrip(" .")

    if path in {"", ".", ".."}:
        path = "_"
    if is_windows_reserved_path_part(path):
        path = f"_{path}"

    encoded = path.encode("utf-8")
    if len(encoded) <= PATH_COMPONENT_MAX_BYTES:
        return path

    marker = f"_{hashlib.sha256(encoded).hexdigest()[:PATH_COMPONENT_HASH_LENGTH]}"
    suffix = Path(path).suffix
    stem = path[: -len(suffix)] if suffix else path
    stem_budget = PATH_COMPONENT_MAX_BYTES - len(marker) - len(suffix.encode("utf-8"))
    if stem_budget <= 0:
        stem = path
        suffix = ""
        stem_budget = PATH_COMPONENT_MAX_BYTES - len(marker)
    shortened_stem = stem.encode("utf-8")[:stem_budget].decode("utf-8", errors="ignore")
    return f"{shortened_stem}{marker}{suffix}"


def sanitized_node_path_parts(node: Node) -> tuple[str, ...]:
    return tuple(
        sanitize_path_part(part)
        for index, part in enumerate(node.get_path())
        if not (index == 0 and part == "")
    )


def windows_extended_length_path(path: str) -> str:
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def with_windows_extended_length_prefix(path: Path, *, force: bool = False) -> Path:
    if not is_windows():
        return path
    absolute_path = os.path.abspath(os.fspath(path))
    if not force and len(absolute_path) < WINDOWS_EXTENDED_PATH_THRESHOLD:
        return path
    return Path(windows_extended_length_path(absolute_path))


def get_sanitized_node_path(node: Node, sync_directory: Path) -> Path:
    sync_directory = sync_directory.expanduser()
    target_path = sync_directory.joinpath(*sanitized_node_path_parts(node))
    resolved_sync_directory = sync_directory.resolve(strict=False)
    resolved_target = target_path.resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_sync_directory):
        raise ValueError(f"Refusing to write outside sync directory: {target_path}")
    return with_windows_extended_length_prefix(target_path)


def parse_conflict_path(path: Path) -> ConflictPathInfo | None:
    match = CONFLICT_RE.match(path.name)
    if match is None:
        match = LEGACY_CONFLICT_RE.match(path.name)
        if match is None:
            return None
        # A legacy name ending in a number is ambiguous: it can mean either an
        # indexed extensionless conflict or an unindexed numeric extension.
        # Cleanup must fail closed rather than risk grouping it with the wrong
        # canonical file.
        if match.group("index") and not match.group("suffix"):
            return None
    index = int(match.group("index") or 0)
    canonical = path.with_name(f"{match.group('stem')}{match.group('suffix')}")
    return ConflictPathInfo(canonical, index)


def format_conflict_path(path: Path, hash_str: str, index: int | None = None) -> Path:
    copy_index = 0 if index is None else index
    return path.with_name(
        f"{path.stem}.{CONFLICT_MARKER}.{hash_str}.copy.{copy_index}{path.suffix}"
    )


def make_conflict_path(path: Path) -> Path:
    """Return a unique path for storing a locally modified file."""
    # Derive a short hash from the current contents to make the filename
    # stable and recognizable while remaining reasonably unique.
    hash_str = "unknown"
    try:
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha1")
            hash_str = digest.hexdigest()[:8]
    except FileNotFoundError:
        hash_str = "missing"

    conflict_path = format_conflict_path(path, hash_str)
    index = 1
    while conflict_path.exists():
        conflict_path = format_conflict_path(path, hash_str, index)
        index += 1
    return with_windows_extended_length_prefix(conflict_path)
