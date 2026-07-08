from __future__ import annotations

import hashlib
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
    r"(?:\.(?P<index>\d+))?"
    r"(?P<suffix>.*)$"
)


@dataclass(frozen=True)
class ConflictPathInfo:
    canonical: Path
    index: int


def sanitize_path_part(path: str) -> str:
    path = urllib.parse.unquote(path)
    path = "".join(
        character
        for character in path
        if character not in INVALID_CHARS and ord(character) >= 32
    )
    path = path.lstrip(" ").rstrip(" .")

    # Folders downloaded from Moodle display amp; in places where an
    # ampersand should be displayed instead. In the web UI, however, the
    # ampersand is shown correctly, and we're trying to emulate that here.
    path = path.replace("amp;", "&")
    if path in {"", ".", ".."}:
        path = "_"
    if is_windows_reserved_path_part(path):
        path = f"_{path}"

    return path


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


def get_sanitized_node_path(node: Node, basedir: Path) -> Path:
    basedir = basedir.expanduser()
    path_segments = []
    for index, part in enumerate(node.get_path()):
        if index == 0 and part == "":
            continue
        sanitized = sanitize_path_part(part)
        path_segments.append(sanitized)

    target_path = basedir.joinpath(*path_segments)
    resolved_basedir = basedir.resolve(strict=False)
    resolved_target = target_path.resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_basedir):
        raise ValueError(f"Refusing to write outside basedir: {target_path}")
    return with_windows_extended_length_prefix(target_path)


def parse_conflict_path(path: Path) -> ConflictPathInfo | None:
    match = CONFLICT_RE.match(path.name)
    if match is None:
        return None
    index = int(match.group("index")) if match.group("index") else 0
    canonical = path.with_name(f"{match.group('stem')}{match.group('suffix')}")
    return ConflictPathInfo(canonical, index)


def format_conflict_path(path: Path, hash_str: str, index: int | None = None) -> Path:
    indexed = "" if index is None else f".{index}"
    return path.with_name(
        f"{path.stem}.{CONFLICT_MARKER}.{hash_str}{indexed}{path.suffix}"
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
