import logging
from pathlib import Path
from typing import Any

from syncmymoodle.constants import COURSE_CACHE_FILENAME
from syncmymoodle.context import SyncContext
from syncmymoodle.node import (
    NAME_CLASH_ID_UNSET,
    DownloadStatus,
    Node,
)
from syncmymoodle.pathing import get_sanitized_node_path
from syncmymoodle.storage import read_private_gzip_json, write_private_gzip_json

logger = logging.getLogger(__name__)


def _node_path(ctx: SyncContext, node: Node) -> Path:
    return get_sanitized_node_path(node, Path(ctx.config.sync_directory))


def match_old_cache_child(old_node: Node | None, child: Node) -> Node | None:
    """Find the previous cache node corresponding to ``child``, if any."""
    if old_node is None:
        return None
    candidates = [
        c for c in old_node.children if c.name == child.name and c.type == child.type
    ]
    if not candidates:
        return None

    for attr in ("url", "name_clash_id", "id"):
        child_value = getattr(child, attr, None)
        if child_value is None or child_value is NAME_CLASH_ID_UNSET:
            continue
        for candidate in candidates:
            if getattr(candidate, attr, None) == child_value:
                return candidate

    return candidates[0]


def node_to_cache_data(
    ctx: SyncContext,
    node: Node,
    old_node: Node | None = None,
) -> dict[str, Any]:
    timemodified = node.timemodified
    etag = node.etag
    etag_kind = node.etag_kind
    content_hash = getattr(node, "content_hash", None)
    remote_size = getattr(node, "remote_size", None)
    is_handled = node.is_handled
    node_path = _node_path(ctx, node)
    downloaded_this_run = node_path in ctx.downloaded_paths
    # If this file was not actually downloaded this run but a previously
    # downloaded version is still on disk, keep the previously cached version
    # markers. The node may still be marked as handled when download traversal
    # skipped an unchanged existing file; downloaded_paths tells us whether
    # bytes were really replaced in this run.
    if (
        not downloaded_this_run
        and old_node is not None
        and old_node.is_handled
        and node_path.exists()
    ):
        timemodified = getattr(old_node, "timemodified", None)
        etag = getattr(old_node, "etag", None)
        etag_kind = getattr(old_node, "etag_kind", None)
        content_hash = getattr(old_node, "content_hash", None)
        remote_size = (
            remote_size
            if remote_size is not None
            else getattr(old_node, "remote_size", None)
        )
        is_handled = True
    return {
        "name": node.name,
        "id": node.id,
        "type": node.type,
        "url": node.url,
        "timemodified": timemodified,
        "etag": etag,
        "etag_kind": str(etag_kind) if etag_kind else None,
        "content_hash": content_hash,
        "remote_size": remote_size,
        "name_clash_id": node.name_clash_id,
        "download_status": str(
            DownloadStatus.HANDLED if is_handled else DownloadStatus.PENDING
        ),
        "children": [
            node_to_cache_data(ctx, child, match_old_cache_child(old_node, child))
            for child in node.children
        ],
    }


def node_from_cache_data(data: dict[str, Any], parent: Node | None = None) -> Node:
    download_status = data.get("download_status")
    legacy_is_downloaded = download_status is None and bool(
        data.get("is_downloaded", False)
    )
    node = Node(
        data.get("name", ""),
        data.get("id"),
        data.get("type", "Unknown"),
        parent,
        url=data.get("url"),
        timemodified=data.get("timemodified"),
        etag=data.get("etag"),
        etag_kind=data.get("etag_kind"),
        content_hash=data.get("content_hash"),
        remote_size=data.get("remote_size"),
        name_clash_id=data.get("name_clash_id", NAME_CLASH_ID_UNSET),
        download_status=download_status,
        is_downloaded=legacy_is_downloaded,
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
    if not hasattr(node, "etag_kind"):
        node.etag_kind = None
    if not hasattr(node, "content_hash"):
        node.content_hash = None
    if not hasattr(node, "remote_size"):
        node.remote_size = None
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
    course_node: Node,
    log: logging.Logger = logger,
) -> Node | None:
    """Load and return the cached course root for the given course node."""
    course_path = _node_path(ctx, course_node)
    if course_path in ctx.course_caches:
        return ctx.course_caches[course_path]

    cache_path = course_path / COURSE_CACHE_FILENAME
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
    node: Node,
    log: logging.Logger = logger,
) -> Node | None:
    """Return the cached node for this node from the course cache, if any."""
    try:
        course_node = get_course_node(node)
    except Exception:
        return None

    cached_course_root = get_course_cache_root(ctx, course_node, log)
    if cached_course_root is None:
        return None

    rel_nodes: list[Node] = []
    cur: Node = node
    while cur is not course_node:
        rel_nodes.insert(0, cur)
        if cur.parent is None:
            return None
        cur = cur.parent
    if not rel_nodes:
        return cached_course_root

    old_node: Node | None = cached_course_root
    for rel_node in rel_nodes:
        old_node = match_old_cache_child(old_node, rel_node)
        if old_node is None:
            return None
    return old_node


def cache_root_node(
    ctx: SyncContext,
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
            course_path = _node_path(ctx, course_node)
            # Read the previous course cache before overwriting it, so we can
            # preserve version markers for files that were not downloaded
            # this run (see node_to_cache_data).
            old_course_root = get_course_cache_root(ctx, course_node, log)
            course_path.mkdir(parents=True, exist_ok=True)
            cache_path = course_path / COURSE_CACHE_FILENAME
            write_private_gzip_json(
                cache_path,
                {
                    "format": "syncmymoodle.course-cache.v1",
                    "course": node_to_cache_data(ctx, course_node, old_course_root),
                },
            )
