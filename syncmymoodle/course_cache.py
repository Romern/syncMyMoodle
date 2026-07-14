import logging
from pathlib import Path
from typing import Any

from syncmymoodle import links
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
COURSE_CACHE_FORMAT = "syncmymoodle.course-cache.v1"
H5P_CONTENT_CACHE_KEY = "h5p_content"


def _node_path(ctx: SyncContext, node: Node) -> Path:
    return get_sanitized_node_path(node, Path(ctx.config.sync_directory))


def _cache_payload(
    ctx: SyncContext,
    course_node: Node,
    log: logging.Logger,
) -> dict[str, Any] | None:
    course_path = _node_path(ctx, course_node)
    if course_path in ctx.course_cache_payloads:
        return ctx.course_cache_payloads[course_path]

    cache_path = course_path / COURSE_CACHE_FILENAME
    payload = (
        read_private_gzip_json(cache_path, "course cache")
        if cache_path.exists()
        else None
    )
    if not isinstance(payload, dict):
        payload = None
    elif payload.get("format") != COURSE_CACHE_FORMAT:
        log.warning("Ignoring unsupported course cache format: %s", cache_path)
        payload = None
    ctx.course_cache_payloads[course_path] = payload
    return payload


def _h5p_content_cache(
    ctx: SyncContext,
    course_node: Node,
    log: logging.Logger,
) -> dict[int, tuple[str, str]]:
    course_path = _node_path(ctx, course_node)
    if course_path in ctx.h5p_content_caches:
        return ctx.h5p_content_caches[course_path]

    cache: dict[int, tuple[str, str]] = {}
    payload = _cache_payload(ctx, course_node, log)
    cached_items = payload.get(H5P_CONTENT_CACHE_KEY) if payload else None
    if isinstance(cached_items, dict):
        for raw_module_id, raw_entry in cached_items.items():
            try:
                module_id = int(raw_module_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_entry, dict):
                continue
            marker = raw_entry.get("marker")
            content = raw_entry.get("content")
            if module_id >= 0 and isinstance(marker, str) and isinstance(content, str):
                cache[module_id] = (marker, content)
    ctx.h5p_content_caches[course_path] = cache
    return cache


def get_h5p_content(
    ctx: SyncContext,
    course_node: Node,
    module_id: int,
    marker: str,
    log: logging.Logger = logger,
) -> str | None:
    """Return cached extracted H5P content when its package is unchanged."""
    cached = _h5p_content_cache(ctx, course_node, log).get(module_id)
    return cached[1] if cached is not None and cached[0] == marker else None


def store_h5p_content(
    ctx: SyncContext,
    course_node: Node,
    module_id: int,
    marker: str,
    content: str,
    log: logging.Logger = logger,
) -> None:
    """Retain extracted H5P content for the next per-course cache write."""
    _h5p_content_cache(ctx, course_node, log)[module_id] = (marker, content)


def match_old_cache_child(old_node: Node | None, child: Node) -> Node | None:
    """Find the previous cache node corresponding to ``child``, if any."""
    if old_node is None:
        return None

    child_youtube_id = links.youtube_video_id_from_node(child)
    if child_youtube_id is not None:
        for candidate in old_node.children:
            if links.youtube_video_id_from_node(candidate) == child_youtube_id:
                return candidate

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
    content_hash = node.content_hash
    remote_size = node.remote_size
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
        timemodified = old_node.timemodified
        etag = old_node.etag
        etag_kind = old_node.etag_kind
        content_hash = old_node.content_hash
        remote_size = remote_size if remote_size is not None else old_node.remote_size
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
    name = data.get("name", "")
    node_type = data.get("type", "Unknown")
    children = data.get("children", [])
    if not isinstance(name, str) or not isinstance(node_type, str):
        raise ValueError("course cache node has invalid name or type")
    if not isinstance(children, list) or not all(
        isinstance(child, dict) for child in children
    ):
        raise ValueError("course cache node has invalid children")

    download_status = data.get("download_status")
    if download_status is None and data.get("is_downloaded"):
        download_status = DownloadStatus.HANDLED
    node = Node(
        name,
        data.get("id"),
        node_type,
        parent,
        url=data.get("url"),
        timemodified=data.get("timemodified"),
        etag=data.get("etag"),
        etag_kind=data.get("etag_kind"),
        content_hash=data.get("content_hash"),
        remote_size=data.get("remote_size"),
        name_clash_id=data.get("name_clash_id", NAME_CLASH_ID_UNSET),
        download_status=download_status,
    )
    node.children = [node_from_cache_data(child, node) for child in children]
    return node


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

    payload = _cache_payload(ctx, course_node, log)
    if payload is None:
        return None
    course_data = payload.get("course")
    if not isinstance(course_data, dict):
        return None

    try:
        cached_course_root = node_from_cache_data(course_data)
    except (TypeError, ValueError):
        log.warning(
            "Ignoring malformed course cache: %s",
            course_path / COURSE_CACHE_FILENAME,
        )
        return None

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

    Each course directory beneath the sync directory receives its own cache file
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
            payload: dict[str, Any] = {
                "format": COURSE_CACHE_FORMAT,
                "course": node_to_cache_data(ctx, course_node, old_course_root),
            }
            h5p_content = _h5p_content_cache(ctx, course_node, log)
            if h5p_content:
                payload[H5P_CONTENT_CACHE_KEY] = {
                    str(module_id): {"marker": marker, "content": content}
                    for module_id, (marker, content) in sorted(h5p_content.items())
                }
            write_private_gzip_json(
                cache_path,
                payload,
            )
            ctx.course_cache_payloads[course_path] = payload
