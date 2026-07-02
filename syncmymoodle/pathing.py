import hashlib
import urllib.parse
from pathlib import Path

from syncmymoodle.node import Node


def sanitize_path_part(path: str, invalid_chars: str) -> str:
    path = urllib.parse.unquote(path)
    path = "".join([s for s in path if s not in invalid_chars])
    while path and path[-1] == " ":
        path = path[:-1]
    while path and path[0] == " ":
        path = path[1:]

    # Folders downloaded from Moodle display amp; in places where an
    # ampersand should be displayed instead. In the web UI, however, the
    # ampersand is shown correctly, and we're trying to emulate that here.
    path = path.replace("amp;", "&")

    return path


def get_sanitized_node_path(node: Node, basedir: Path, invalid_chars: str) -> Path:
    basedir = basedir.expanduser()
    path_segments = []
    for part in node.get_path():
        if part == "":
            continue
        sanitized = sanitize_path_part(part, invalid_chars)
        if sanitized in {"", ".", ".."}:
            sanitized = "_"
        path_segments.append(sanitized)

    target_path = basedir.joinpath(*path_segments)
    resolved_basedir = basedir.resolve(strict=False)
    resolved_target = target_path.resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_basedir):
        raise ValueError(f"Refusing to write outside basedir: {target_path}")
    return target_path


def make_conflict_path(path: Path) -> Path:
    """Return a unique path for storing a locally modified file."""
    suffix = path.suffix
    stem = path.stem

    # Derive a short hash from the current contents to make the filename
    # stable and recognizable while remaining reasonably unique.
    hash_str = "unknown"
    try:
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha1")
            hash_str = digest.hexdigest()[:8]
    except FileNotFoundError:
        hash_str = "missing"

    conflict_path = path.with_name(f"{stem}.syncconflict.{hash_str}{suffix}")
    index = 1
    while conflict_path.exists():
        conflict_path = path.with_name(
            f"{stem}.syncconflict.{hash_str}.{index}{suffix}"
        )
        index += 1
    return conflict_path
