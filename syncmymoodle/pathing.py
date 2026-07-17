from __future__ import annotations

import base64
import hashlib
import html
import ntpath
import os
import re
import stat
import sys
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from syncmymoodle.constants import INVALID_CHARS
from syncmymoodle.node import DownloadKind, Node, NodeKind

WINDOWS_EXTENDED_PATH_THRESHOLD = 240
# Leave room for download sidecars and ``.syncconflict`` suffixes while staying
# below the 255-byte component limit used by common Linux and macOS filesystems.
# A UTF-8 byte limit is also conservative for NTFS's 255 UTF-16-code-unit limit.
PATH_COMPONENT_MAX_BYTES = 220
PATH_COMPONENT_HASH_LENGTH = 8


class UnsafeInternalPathError(ValueError):
    """An internal control path could escape or traverse a filesystem link."""


def _is_link_or_reparse_point(result: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(result, "st_file_attributes", 0)
    return stat.S_ISLNK(result.st_mode) or bool(attributes & reparse_flag)


@dataclass(frozen=True)
class InternalPathRoot:
    """A resolved sync root that rejects linked internal descendants."""

    root: Path

    @classmethod
    def resolve(cls, configured_root: Path | InternalPathRoot) -> InternalPathRoot:
        if isinstance(configured_root, InternalPathRoot):
            return configured_root
        return cls(configured_root.expanduser().resolve(strict=False))

    def path(self, *parts: str) -> Path:
        """Build and validate an internal path below this root."""
        return self.require(self.root.joinpath(*parts))

    def require(self, path: Path) -> Path:
        """Validate one path without following any descendant links."""
        candidate = absolute_path(path)
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise UnsafeInternalPathError(
                f"Refusing internal path outside sync directory: {candidate}"
            ) from error

        current = self.root
        for part in relative.parts:
            current /= part
            try:
                result = current.lstat()
            except FileNotFoundError:
                break
            except OSError as error:
                raise UnsafeInternalPathError(
                    f"Could not validate internal path: {current}"
                ) from error
            if _is_link_or_reparse_point(result):
                raise UnsafeInternalPathError(
                    f"Refusing linked internal path: {current}"
                )

        try:
            resolved = candidate.resolve(strict=False)
        except OSError as error:
            raise UnsafeInternalPathError(
                f"Could not resolve internal path: {candidate}"
            ) from error
        if not resolved.is_relative_to(self.root):
            raise UnsafeInternalPathError(
                f"Refusing internal path outside sync directory: {candidate}"
            )
        return candidate

    def create_parent(self, path: Path) -> Path:
        """Create a validated path's parents without accepting linked components."""
        candidate = self.require(path)
        self.root.mkdir(parents=True, exist_ok=True)
        current = self.root
        relative_parent = candidate.parent.relative_to(self.root)
        for part in relative_parent.parts:
            current /= part
            try:
                current.mkdir()
            except FileExistsError:
                pass
            self.require(current)
        return self.require(candidate)


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
    path = unicodedata.normalize("NFC", path)
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


def _clash_suffix(node: Node) -> str:
    # A URL identifies the actual downloadable file, including direct-link
    # nodes whose name_clash_id is None. Non-downloadable nodes such as courses
    # fall back to their stable Moodle id.
    key = node.url if node.url is not None else node.name_clash_id
    digest = hashlib.md5(str(key).encode("utf-8"), usedforsecurity=False).hexdigest()
    return base64.urlsafe_b64encode(digest.encode("utf-8")).decode()[:10]


def _stable_clash_name(node: Node) -> str:
    filename = Path(node.name)
    return f"{filename.stem}_{_clash_suffix(node)}{filename.suffix}"


def _same_url_clash_name(node: Node) -> str:
    filename = Path(node.name)
    identity = (
        node.url,
        node.name,
        node.id,
        node.type,
        node.name_clash_id,
        node.download_kind,
    )
    digest = hashlib.md5(
        repr(identity).encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    suffix = base64.urlsafe_b64encode(digest.encode("utf-8")).decode()[:10]
    return f"{filename.stem}_{suffix}{filename.suffix}"


def _opencast_clash_name(node: Node) -> str:
    return f"{Path(node.name).name}_{str(node.url).split('/')[-1]}"


def _filesystem_name_key(node: Node) -> str:
    return sanitize_path_part(node.name).casefold()


def _filesystem_path_key(node: Node) -> tuple[str, ...]:
    return tuple(part.casefold() for part in sanitized_node_path_parts(node))


def _general_name_clash(left: Node, right: Node) -> bool:
    if _filesystem_name_key(left) != _filesystem_name_key(right):
        return False
    if left.url != right.url:
        return True
    if left.url is not None:
        return True
    return (
        left.type == NodeKind.COURSE
        and right.type == NodeKind.COURSE
        and left.name_clash_id != right.name_clash_id
    )


def _apply_opencast_name_clashes(children: list[Node]) -> list[Node]:
    remaining = children.copy()
    renamed: list[Node] = []

    while remaining:
        child = remaining.pop(0)
        renamed.append(child)
        if child.download_kind is not DownloadKind.OPENCAST:
            continue

        siblings = [
            sibling
            for sibling in remaining
            if _filesystem_name_key(sibling) == _filesystem_name_key(child)
            and sibling.url != child.url
        ]
        if not siblings:
            continue

        child.name = _opencast_clash_name(child)
        for sibling in siblings:
            sibling.name = _opencast_clash_name(sibling)
            remaining.remove(sibling)
            renamed.append(sibling)

    return renamed


def _apply_general_name_clashes(children: list[Node]) -> list[Node]:
    remaining = children.copy()
    renamed: list[Node] = []

    while remaining:
        child = remaining.pop(0)
        renamed.append(child)
        siblings = [
            sibling for sibling in remaining if _general_name_clash(child, sibling)
        ]
        if not siblings:
            continue

        clashing_nodes = [child, *siblings]
        names = [
            (
                _same_url_clash_name(node)
                if node.url is not None
                and sum(other.url == node.url for other in clashing_nodes) > 1
                else _stable_clash_name(node)
            )
            for node in clashing_nodes
        ]
        child.name = names[0]
        for sibling, name in zip(siblings, names[1:], strict=True):
            sibling.name = name
            remaining.remove(sibling)
            renamed.append(sibling)

    return renamed


def _resolve_sibling_name_clashes(node: Node) -> None:
    node.children = _apply_opencast_name_clashes(node.children)
    node.children = _apply_general_name_clashes(node.children)
    for child in node.children:
        _resolve_sibling_name_clashes(child)


def _resolve_download_path_clashes(root: Node) -> None:
    download_nodes: list[Node] = []
    remaining = [root]
    while remaining:
        node = remaining.pop()
        remaining.extend(node.children)
        if node.url:
            download_nodes.append(node)

    # Each pass either resolves every collision or moves a colliding file away
    # from a pre-existing generated name. At most one such name can be consumed
    # per file; the bound prevents a malformed tree from looping.
    for _ in range(len(download_nodes) + 1):
        nodes_by_path: dict[tuple[str, ...], list[Node]] = {}
        for node in download_nodes:
            nodes_by_path.setdefault(_filesystem_path_key(node), []).append(node)
        clashes = [
            nodes
            for nodes in nodes_by_path.values()
            if len({node.url for node in nodes}) > 1
        ]
        if not clashes:
            return
        for nodes in clashes:
            for node in nodes:
                node.name = _stable_clash_name(node)
    raise ValueError("Could not create unique paths for downloaded files")


def resolve_node_path_clashes(root: Node) -> None:
    """Make every materialized node path unique on supported filesystems."""
    _resolve_sibling_name_clashes(root)
    _resolve_download_path_clashes(root)


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
