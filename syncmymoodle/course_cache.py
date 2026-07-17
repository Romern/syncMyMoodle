import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from syncmymoodle import links, opencast
from syncmymoodle.constants import COURSE_CACHE_FILENAME
from syncmymoodle.context import LinkedResourceCacheEntry, SyncContext
from syncmymoodle.http_utils import HTML_CONTENT_TYPES, normalized_http_origin
from syncmymoodle.node import (
    NAME_CLASH_ID_UNSET,
    DownloadKind,
    DownloadStatus,
    Node,
)
from syncmymoodle.pathing import get_sanitized_node_path
from syncmymoodle.storage import read_private_gzip_json, write_private_gzip_json

logger = logging.getLogger(__name__)
COURSE_CACHE_FORMAT = "syncmymoodle.course-cache.v1"
MODULE_CACHE_KEY = "module_data"
CACHED_TEXT_CACHE_KEY = "cached_text"
OPENCAST_EPISODES_CACHE_KEY = "opencast_episodes"
OPENCAST_EPISODES_CACHE_FORMAT = "syncmymoodle.opencast-episodes.v1"
LINKED_RESOURCES_CACHE_KEY = "linked_resources"
LINKED_RESOURCES_CACHE_FORMAT = "syncmymoodle.linked-resources.v1"
H5P_CONTENT_KIND = "h5p"
PAGE_CONTENT_KIND = "page"
CACHED_TEXT_KINDS = (H5P_CONTENT_KIND, PAGE_CONTENT_KIND)
LEGACY_DOWNLOAD_KINDS = {
    "Youtube": DownloadKind.YOUTUBE,
    "Emedia": DownloadKind.EMEDIA,
    "Quiz": DownloadKind.QUIZ,
    "Opencast": DownloadKind.OPENCAST,
}


@dataclass(frozen=True)
class CachedTextEntry:
    marker: str
    content: str
    base_url: str | None = None


@dataclass(frozen=True)
class AssignmentCacheEntry:
    since: int
    files: list[dict[str, Any]]


@dataclass(frozen=True)
class QuizCacheEntry:
    since: int
    attempts: list[dict[str, Any]]
    reviews: dict[int, dict[str, Any]]
    timeclose: int
    refresh_after: int | None


@dataclass
class CourseCacheState:
    course_root: Node | None = None
    cached_text: dict[str, dict[int, CachedTextEntry]] = field(
        default_factory=lambda: {kind: {} for kind in CACHED_TEXT_KINDS}
    )
    assignments: dict[int, AssignmentCacheEntry] = field(default_factory=dict)
    quizzes: dict[int, QuizCacheEntry] = field(default_factory=dict)
    opencast_episodes: dict[str, opencast.OpencastEpisode] = field(default_factory=dict)
    linked_resources: dict[str, LinkedResourceCacheEntry] = field(default_factory=dict)
    complete_module_inventory: bool = False


def _node_path(ctx: SyncContext, node: Node) -> Path:
    return get_sanitized_node_path(node, Path(ctx.config.sync_directory))


def _module_id(value: Any) -> int | None:
    try:
        module_id = int(value)
    except (TypeError, ValueError):
        return None
    return module_id if module_id > 0 else None


def _cache_since(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _dict_list(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return [dict(item) for item in value]


def _cached_text_entries(
    value: Any,
    kind: str,
) -> dict[int, CachedTextEntry]:
    entries: dict[int, CachedTextEntry] = {}
    if not isinstance(value, dict):
        return entries
    for raw_module_id, raw_entry in value.items():
        module_id = _module_id(raw_module_id)
        if module_id is None or not isinstance(raw_entry, dict):
            continue
        marker = raw_entry.get("marker")
        content = raw_entry.get("content")
        base_url = raw_entry.get("url")
        base_url_valid = isinstance(base_url, str) and bool(base_url)
        if (
            isinstance(marker, str)
            and marker
            and isinstance(content, str)
            and (kind != PAGE_CONTENT_KIND or base_url_valid)
        ):
            entries[module_id] = CachedTextEntry(
                marker,
                content,
                base_url if base_url_valid else None,
            )
    return entries


def _assignment_cache_entries(value: Any) -> dict[int, AssignmentCacheEntry]:
    entries: dict[int, AssignmentCacheEntry] = {}
    if not isinstance(value, dict):
        return entries
    for raw_module_id, raw_entry in value.items():
        module_id = _module_id(raw_module_id)
        if module_id is None or not isinstance(raw_entry, dict):
            continue
        since = _cache_since(raw_entry.get("since"))
        files = _dict_list(raw_entry.get("files"))
        if since is not None and files is not None:
            entries[module_id] = AssignmentCacheEntry(since, files)
    return entries


def _quiz_reviews(value: Any) -> dict[int, dict[str, Any]] | None:
    if not isinstance(value, dict):
        return None
    reviews: dict[int, dict[str, Any]] = {}
    for raw_attempt_id, review in value.items():
        attempt_id = _module_id(raw_attempt_id)
        if attempt_id is None or not isinstance(review, dict):
            return None
        reviews[attempt_id] = dict(review)
    return reviews


def _quiz_cache_entries(value: Any) -> dict[int, QuizCacheEntry]:
    entries: dict[int, QuizCacheEntry] = {}
    if not isinstance(value, dict):
        return entries
    for raw_module_id, raw_entry in value.items():
        module_id = _module_id(raw_module_id)
        if module_id is None or not isinstance(raw_entry, dict):
            continue
        since = _cache_since(raw_entry.get("since"))
        attempts = _dict_list(raw_entry.get("attempts"))
        reviews = _quiz_reviews(raw_entry.get("reviews"))
        timeclose = _cache_since(raw_entry.get("timeclose"))
        if "refresh_after" not in raw_entry:
            continue
        refresh_after = raw_entry.get("refresh_after")
        if refresh_after is not None:
            refresh_after = _cache_since(refresh_after)
            if refresh_after is None:
                continue
        if (
            since is not None
            and attempts is not None
            and reviews is not None
            and timeclose is not None
        ):
            entries[module_id] = QuizCacheEntry(
                since,
                attempts,
                reviews,
                timeclose,
                refresh_after,
            )
    return entries


def _opencast_episode_entries(
    value: Any,
) -> dict[str, opencast.OpencastEpisode]:
    entries: dict[str, opencast.OpencastEpisode] = {}
    if (
        not isinstance(value, dict)
        or value.get("format") != OPENCAST_EPISODES_CACHE_FORMAT
        or not isinstance(value.get("episodes"), dict)
    ):
        return entries
    for episode_id, raw_episode in value["episodes"].items():
        if not isinstance(episode_id, str) or not episode_id:
            continue
        episode = opencast.episode_from_cache_data(raw_episode)
        if episode is not None:
            entries[episode_id] = episode
    return entries


def _linked_resource_entries(value: Any) -> dict[str, LinkedResourceCacheEntry]:
    entries: dict[str, LinkedResourceCacheEntry] = {}
    if (
        not isinstance(value, dict)
        or value.get("format") != LINKED_RESOURCES_CACHE_FORMAT
        or not isinstance(value.get("resources"), dict)
    ):
        return entries
    for requested_url, raw_entry in value["resources"].items():
        if (
            not isinstance(requested_url, str)
            or normalized_http_origin(requested_url) is None
        ):
            continue
        if not isinstance(raw_entry, dict):
            continue
        final_url = raw_entry.get("final_url")
        content_type = raw_entry.get("content_type")
        html = raw_entry.get("html")
        etag = raw_entry.get("etag")
        last_modified = raw_entry.get("last_modified")
        fresh_until = raw_entry.get("fresh_until")
        remote_size = raw_entry.get("remote_size")
        if (
            not isinstance(final_url, str)
            or normalized_http_origin(final_url) is None
            or not isinstance(content_type, str)
            or not content_type
            or (html is not None and not isinstance(html, str))
            or ((content_type in HTML_CONTENT_TYPES) != isinstance(html, str))
            or (etag is not None and (not isinstance(etag, str) or not etag))
            or (
                last_modified is not None
                and (not isinstance(last_modified, str) or not last_modified)
            )
            or (
                fresh_until is not None
                and (
                    not isinstance(fresh_until, (int, float))
                    or isinstance(fresh_until, bool)
                    or not math.isfinite(fresh_until)
                    or fresh_until < 0
                )
            )
            or (
                remote_size is not None
                and (
                    not isinstance(remote_size, int)
                    or isinstance(remote_size, bool)
                    or remote_size < 0
                )
            )
        ):
            continue
        entries[requested_url] = LinkedResourceCacheEntry(
            final_url,
            content_type,
            html,
            etag,
            last_modified,
            float(fresh_until) if fresh_until is not None else None,
            remote_size,
        )
    return entries


def _course_linked_resources(
    ctx: SyncContext,
    course_id: int | None,
    raw_cache: dict[str, Any],
    personal_cache_matches: bool,
) -> dict[str, LinkedResourceCacheEntry]:
    linked_resources = (
        _linked_resource_entries(raw_cache.get(LINKED_RESOURCES_CACHE_KEY))
        if personal_cache_matches
        else {}
    )
    if course_id is None:
        return linked_resources
    course_key = str(course_id)
    runtime_resources = ctx.linked_resources_by_course.get(course_key)
    if runtime_resources is None:
        ctx.linked_resources_by_course[course_key] = linked_resources
        return linked_resources
    for requested_url, entry in linked_resources.items():
        runtime_resources.setdefault(requested_url, entry)
    return runtime_resources


def _course_cache_state(
    ctx: SyncContext,
    course_node: Node,
    log: logging.Logger,
) -> CourseCacheState:
    if course_node in ctx.course_cache_states:
        return ctx.course_cache_states[course_node]

    course_path = _node_path(ctx, course_node)
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

    course_root = None
    course_data = payload.get("course") if payload else None
    if isinstance(course_data, dict):
        try:
            course_root = node_from_cache_data(course_data)
        except (TypeError, ValueError):
            log.warning("Ignoring malformed course cache: %s", cache_path)

    raw_cache = payload.get(MODULE_CACHE_KEY) if payload else None
    raw_cache = raw_cache if isinstance(raw_cache, dict) else {}
    raw_cached_text = raw_cache.get(CACHED_TEXT_CACHE_KEY)
    raw_cached_text = raw_cached_text if isinstance(raw_cached_text, dict) else {}
    current_user_id = (
        ctx.moodle_account.user_id if ctx.moodle_account is not None else None
    )
    personal_cache_matches = (
        current_user_id is not None
        and raw_cache.get("owner_user_id") == current_user_id
    )
    course_id = _module_id(course_node.id)
    linked_resources = _course_linked_resources(
        ctx,
        course_id,
        raw_cache,
        personal_cache_matches,
    )
    state = CourseCacheState(
        course_root=course_root,
        cached_text={
            kind: _cached_text_entries(raw_cached_text.get(kind), kind)
            for kind in CACHED_TEXT_KINDS
        },
        assignments=(
            _assignment_cache_entries(raw_cache.get("assignments"))
            if personal_cache_matches
            else {}
        ),
        quizzes=(
            _quiz_cache_entries(raw_cache.get("quizzes"))
            if personal_cache_matches
            else {}
        ),
        opencast_episodes=(
            _opencast_episode_entries(raw_cache.get(OPENCAST_EPISODES_CACHE_KEY))
            if personal_cache_matches
            else {}
        ),
        linked_resources=linked_resources,
    )
    if course_id is not None:
        for episode_id, episode in state.opencast_episodes.items():
            ctx.opencast_episode_cache.setdefault(
                (str(course_id), episode_id),
                episode,
            )
    ctx.course_cache_states[course_node] = state
    return state


def get_cached_text(
    ctx: SyncContext,
    course_node: Node,
    kind: str,
    module_id: int,
    marker: str,
    log: logging.Logger = logger,
) -> CachedTextEntry | None:
    entry = _course_cache_state(ctx, course_node, log).cached_text[kind].get(module_id)
    return entry if entry is not None and entry.marker == marker else None


def store_cached_text(
    ctx: SyncContext,
    course_node: Node,
    kind: str,
    module_id: int,
    marker: str,
    content: str,
    base_url: str | None = None,
    log: logging.Logger = logger,
) -> None:
    if kind == PAGE_CONTENT_KIND and not base_url:
        raise ValueError("cached page content requires its base URL")
    _course_cache_state(ctx, course_node, log).cached_text[kind][module_id] = (
        CachedTextEntry(marker, content, base_url)
    )


def get_assignment_cache_entry(
    ctx: SyncContext,
    course_node: Node,
    module_id: int,
    log: logging.Logger = logger,
) -> AssignmentCacheEntry | None:
    return _course_cache_state(ctx, course_node, log).assignments.get(module_id)


def store_assignment_cache_entry(
    ctx: SyncContext,
    course_node: Node,
    module_id: int,
    files: list[dict[str, Any]],
    log: logging.Logger = logger,
) -> None:
    since = ctx.moodle_update_watermark
    if since is None:
        return
    _course_cache_state(ctx, course_node, log).assignments[module_id] = (
        AssignmentCacheEntry(since, files)
    )


def discard_assignment_cache_entry(
    ctx: SyncContext,
    course_node: Node,
    module_id: int,
    log: logging.Logger = logger,
) -> None:
    _course_cache_state(ctx, course_node, log).assignments.pop(module_id, None)


def get_quiz_cache_entry(
    ctx: SyncContext,
    course_node: Node,
    module_id: int,
    log: logging.Logger = logger,
) -> QuizCacheEntry | None:
    return _course_cache_state(ctx, course_node, log).quizzes.get(module_id)


def store_quiz_cache_entry(
    ctx: SyncContext,
    course_node: Node,
    module_id: int,
    attempts: list[dict[str, Any]],
    reviews: dict[int, dict[str, Any]],
    timeclose: int,
    refresh_after: int | None,
    log: logging.Logger = logger,
) -> None:
    since = ctx.moodle_update_watermark
    if since is None:
        return
    _course_cache_state(ctx, course_node, log).quizzes[module_id] = QuizCacheEntry(
        since,
        attempts,
        reviews,
        timeclose,
        refresh_after,
    )


def discard_quiz_cache_entry(
    ctx: SyncContext,
    course_node: Node,
    module_id: int,
    log: logging.Logger = logger,
) -> None:
    _course_cache_state(ctx, course_node, log).quizzes.pop(module_id, None)


def retain_current_modules(
    ctx: SyncContext,
    course_node: Node,
    modules: list[dict[str, Any]],
    log: logging.Logger = logger,
) -> None:
    """Discard cached data for modules no longer present in the course."""
    module_ids: dict[str, set[int]] = {
        "h5pactivity": set(),
        "page": set(),
        "assign": set(),
        "quiz": set(),
    }
    for module in modules:
        module_id = _module_id(module.get("id"))
        modname = module.get("modname")
        if module_id is not None and modname in module_ids:
            module_ids[modname].add(module_id)

    state = _course_cache_state(ctx, course_node, log)
    state.complete_module_inventory = True
    caches = (
        (state.cached_text[H5P_CONTENT_KIND], module_ids["h5pactivity"]),
        (state.cached_text[PAGE_CONTENT_KIND], module_ids["page"]),
        (state.assignments, module_ids["assign"]),
        (state.quizzes, module_ids["quiz"]),
    )
    for cache, current_ids in caches:
        for module_id in cache.keys() - current_ids:
            del cache[module_id]


def _cached_text_data(
    entries: dict[int, CachedTextEntry],
    kind: str,
) -> dict[str, Any]:
    return {
        str(module_id): {
            "marker": entry.marker,
            "content": entry.content,
            **({"url": entry.base_url} if kind == PAGE_CONTENT_KIND else {}),
        }
        for module_id, entry in sorted(entries.items())
    }


def _opencast_episodes_data(
    entries: dict[str, opencast.OpencastEpisode],
) -> dict[str, Any]:
    return {
        "format": OPENCAST_EPISODES_CACHE_FORMAT,
        "episodes": {
            episode_id: opencast.episode_cache_data(episode)
            for episode_id, episode in sorted(entries.items())
        },
    }


def _linked_resources_data(
    entries: dict[str, LinkedResourceCacheEntry],
) -> dict[str, Any]:
    return {
        "format": LINKED_RESOURCES_CACHE_FORMAT,
        "resources": {
            requested_url: {
                "final_url": entry.final_url,
                "content_type": entry.content_type,
                **({"html": entry.html} if entry.html is not None else {}),
                **({"etag": entry.etag} if entry.etag is not None else {}),
                **(
                    {"last_modified": entry.last_modified}
                    if entry.last_modified is not None
                    else {}
                ),
                **(
                    {"fresh_until": entry.fresh_until}
                    if entry.fresh_until is not None
                    else {}
                ),
                **(
                    {"remote_size": entry.remote_size}
                    if entry.remote_size is not None
                    else {}
                ),
            }
            for requested_url, entry in sorted(entries.items())
        },
    }


def _course_module_cache_data(
    ctx: SyncContext,
    state: CourseCacheState,
    course_node: Node,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    course_id = _module_id(course_node.id)
    if course_id is not None:
        course_key = str(course_id)
        state.opencast_episodes = {
            episode_id: episode
            for (
                cached_course_id,
                episode_id,
            ), episode in ctx.opencast_episode_cache.items()
            if cached_course_id == course_key
            and (course_key, episode_id) in ctx.opencast_seen_episodes
        }
        if state.complete_module_inventory:
            seen_urls = {
                requested_url
                for seen_course, requested_url in ctx.seen_linked_resources
                if seen_course == course_key
            }
            state.linked_resources = {
                requested_url: entry
                for requested_url, entry in state.linked_resources.items()
                if requested_url in seen_urls
            }
            ctx.linked_resources_by_course[course_key] = state.linked_resources
    cached_text = {
        kind: _cached_text_data(state.cached_text[kind], kind)
        for kind in CACHED_TEXT_KINDS
        if state.cached_text[kind]
    }
    if cached_text:
        data[CACHED_TEXT_CACHE_KEY] = cached_text
    if (
        state.assignments
        or state.quizzes
        or state.opencast_episodes
        or state.linked_resources
    ):
        if ctx.moodle_account is None:
            return data
        data["owner_user_id"] = ctx.moodle_account.user_id
    if state.assignments:
        data["assignments"] = {
            str(module_id): {"since": entry.since, "files": entry.files}
            for module_id, entry in sorted(state.assignments.items())
        }
    if state.quizzes:
        data["quizzes"] = {
            str(module_id): {
                "since": entry.since,
                "attempts": entry.attempts,
                "timeclose": entry.timeclose,
                "refresh_after": entry.refresh_after,
                "reviews": {
                    str(attempt_id): review
                    for attempt_id, review in sorted(entry.reviews.items())
                },
            }
            for module_id, entry in sorted(state.quizzes.items())
        }
    if state.opencast_episodes:
        data[OPENCAST_EPISODES_CACHE_KEY] = _opencast_episodes_data(
            state.opencast_episodes
        )
    if state.linked_resources:
        data[LINKED_RESOURCES_CACHE_KEY] = _linked_resources_data(
            state.linked_resources
        )
    return data


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
        "download_kind": str(node.download_kind),
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
        download_kind=data.get("download_kind")
        or LEGACY_DOWNLOAD_KINDS.get(node_type, DownloadKind.DIRECT),
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
    return _course_cache_state(ctx, course_node, log).course_root


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
            state = _course_cache_state(ctx, course_node, log)
            course_path.mkdir(parents=True, exist_ok=True)
            cache_path = course_path / COURSE_CACHE_FILENAME
            payload: dict[str, Any] = {
                "format": COURSE_CACHE_FORMAT,
                "course": node_to_cache_data(ctx, course_node, state.course_root),
            }
            module_cache = _course_module_cache_data(ctx, state, course_node)
            if module_cache:
                payload[MODULE_CACHE_KEY] = module_cache
            write_private_gzip_json(cache_path, payload)
            state.course_root = node_from_cache_data(payload["course"])
