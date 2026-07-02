import logging
from pathlib import Path
from typing import Any

from syncmymoodle.context import SyncContext
from syncmymoodle.node import NAME_CLASH_ID_UNSET, Node
from syncmymoodle.pathing import get_sanitized_node_path
from syncmymoodle.storage import read_private_gzip_json, write_private_gzip_json

logger = logging.getLogger(__name__)


def _node_path(ctx: SyncContext, invalid_chars: str, node: Node) -> Path:
    return get_sanitized_node_path(
        node, Path(ctx.config.get("basedir", "./")), invalid_chars
    )


def match_old_cache_child(old_node: Node | None, child: Node) -> Node | None:
    """Find the previous cache node corresponding to ``child``, if any."""
    if old_node is None:
        return None
    candidates = [
        c for c in old_node.children if c.name == child.name and c.type == child.type
    ]
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.url == child.url:
            return candidate
    return candidates[0]


def node_to_cache_data(
    ctx: SyncContext,
    invalid_chars: str,
    node: Node,
    old_node: Node | None = None,
) -> dict[str, Any]:
    timemodified = node.timemodified
    etag = node.etag
    is_downloaded = node.is_downloaded
    # If this file was not (re)downloaded this run but a previously
    # downloaded version is still on disk, keep the previously cached version
    # markers. Otherwise the cache would record Moodle's new timemodified/etag
    # for a file we never actually fetched, which either skips the file
    # forever or moves the on-disk copy aside as a spurious conflict on the
    # next run's retry.
    if (
        not node.is_downloaded
        and old_node is not None
        and getattr(old_node, "is_downloaded", False)
        and _node_path(ctx, invalid_chars, node).exists()
    ):
        timemodified = getattr(old_node, "timemodified", None)
        etag = getattr(old_node, "etag", None)
        is_downloaded = True
    return {
        "name": node.name,
        "id": node.id,
        "type": node.type,
        "url": node.url,
        "timemodified": timemodified,
        "etag": etag,
        "name_clash_id": node.name_clash_id,
        "is_downloaded": is_downloaded,
        "children": [
            node_to_cache_data(
                ctx, invalid_chars, child, match_old_cache_child(old_node, child)
            )
            for child in node.children
        ],
    }


def node_from_cache_data(data: dict[str, Any], parent: Node | None = None) -> Node:
    node = Node(
        data.get("name", ""),
        data.get("id"),
        data.get("type", "Unknown"),
        parent,
        url=data.get("url"),
        timemodified=data.get("timemodified"),
        etag=data.get("etag"),
        name_clash_id=data.get("name_clash_id", NAME_CLASH_ID_UNSET),
        is_downloaded=data.get("is_downloaded", False),
    )
    node.children = [
        node_from_cache_data(child, node)
        for child in data.get("children", [])
        if isinstance(child, dict)
    ]
    return node


def ensure_timemodified_attribute(node: Node) -> None:
    # Old cached root nodes might not have the timemodified attribute yet.
    if not hasattr(node, "timemodified"):
        node.timemodified = None
    if not hasattr(node, "etag"):
        node.etag = None
    if not hasattr(node, "name_clash_id"):
        node.name_clash_id = getattr(node, "id", None)
    for child in getattr(node, "children", []):
        ensure_timemodified_attribute(child)


def get_course_node(node: Node) -> Node:
    """Return the enclosing course node for the given node."""
    cur = node
    while cur is not None and cur.parent is not None:
        if cur.type == "Course":
            return cur
        cur = cur.parent
    raise Exception("Node is not part of a course subtree")


def get_course_cache_root(
    ctx: SyncContext,
    invalid_chars: str,
    course_node: Node,
    log: logging.Logger = logger,
) -> Node | None:
    """Load and return the cached course root for the given course node."""
    course_path = _node_path(ctx, invalid_chars, course_node)
    if course_path in ctx.course_caches:
        return ctx.course_caches[course_path]

    cache_path = course_path / ".syncmymoodle_cache"
    if not cache_path.exists():
        return None

    payload = read_private_gzip_json(cache_path, "course cache")
    if not isinstance(payload, dict):
        return None
    if payload.get("format") != "syncmymoodle.course-cache.v1":
        log.warning("Ignoring unsupported course cache format: %s", cache_path)
        return None
    course_data = payload.get("course")
    if not isinstance(course_data, dict):
        return None

    cached_course_root = node_from_cache_data(course_data)
    ensure_timemodified_attribute(cached_course_root)

    ctx.course_caches[course_path] = cached_course_root
    return cached_course_root


def get_old_node_for(
    ctx: SyncContext,
    invalid_chars: str,
    node: Node,
    log: logging.Logger = logger,
) -> Node | None:
    """Return the cached node for this node from the course cache, if any."""
    try:
        course_node = get_course_node(node)
    except Exception:
        return None

    cached_course_root = get_course_cache_root(ctx, invalid_chars, course_node, log)
    if cached_course_root is None:
        return None

    full_path = node.get_path()
    course_path = course_node.get_path()
    # Compute the path segments beneath the course root
    rel_segments = full_path[len(course_path) :]
    if not rel_segments:
        return cached_course_root

    try:
        return cached_course_root.go_to_path(rel_segments)
    except Exception:
        return None


def cache_root_node(
    ctx: SyncContext,
    invalid_chars: str,
    log: logging.Logger = logger,
) -> None:
    """Persist per-course caches into .syncmymoodle_cache files.

    Each course directory beneath basedir receives its own cache file
    containing the course subtree, which makes caching less brittle than
    a single global root cache.
    """
    if not ctx.root_node:
        return

    for semester_node in ctx.root_node.children:
        if semester_node.type != "Semester":
            continue
        for course_node in semester_node.children:
            if course_node.type != "Course":
                continue
            course_path = _node_path(ctx, invalid_chars, course_node)
            # Read the previous course cache before overwriting it, so we can
            # preserve version markers for files that were not downloaded
            # this run (see node_to_cache_data).
            old_course_root = get_course_cache_root(
                ctx, invalid_chars, course_node, log
            )
            course_path.mkdir(parents=True, exist_ok=True)
            cache_path = course_path / ".syncmymoodle_cache"
            write_private_gzip_json(
                cache_path,
                {
                    "format": "syncmymoodle.course-cache.v1",
                    "course": node_to_cache_data(
                        ctx, invalid_chars, course_node, old_course_root
                    ),
                },
            )
