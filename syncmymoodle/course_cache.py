import hashlib
import logging
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from syncmymoodle import links, opencast
from syncmymoodle.constants import COURSE_CACHE_DIRECTORY, COURSE_CACHE_FILENAME
from syncmymoodle.context import SyncContext
from syncmymoodle.moodle_tokens import normalized_site
from syncmymoodle.node import (
    NAME_CLASH_ID_UNSET,
    DownloadKind,
    DownloadStatus,
    Node,
    NodeKind,
)
from syncmymoodle.pathing import (
    InternalPathRoot,
    get_sanitized_node_path,
    sanitized_node_path_parts,
    with_windows_extended_length_prefix,
)
from syncmymoodle.storage import read_private_gzip_json, write_private_gzip_json

logger = logging.getLogger(__name__)
LEGACY_COURSE_CACHE_FORMAT = "syncmymoodle.course-cache.v1"
COURSE_CACHE_FORMAT = "syncmymoodle.course-cache.v2"
MODULE_CACHE_KEY = "module_data"
CACHED_TEXT_CACHE_KEY = "cached_text"
OPENCAST_EPISODES_CACHE_KEY = "opencast_episodes"
LINKED_RESOURCES_CACHE_KEY = "linked_resources"
H5P_CONTENT_KIND = "h5p"
PAGE_CONTENT_KIND = "page"
CACHED_TEXT_KINDS = (H5P_CONTENT_KIND, PAGE_CONTENT_KIND)
LEGACY_DOWNLOAD_KINDS = {
    "Youtube": DownloadKind.YOUTUBE,
    "Quiz": DownloadKind.QUIZ,
    "Opencast": DownloadKind.OPENCAST,
}
LEGACY_NODE_FIELDS = frozenset(
    {
        "name",
        "id",
        "type",
        "url",
        "timemodified",
        "etag",
        "etag_kind",
        "content_hash",
        "name_clash_id",
        "download_status",
        "children",
    }
)


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
    complete_module_inventory: bool = False


def _node_path(ctx: SyncContext, node: Node) -> Path:
    return get_sanitized_node_path(node, Path(ctx.config.sync_directory))


def _node_artifact_paths(
    ctx: SyncContext,
    node: Node,
    metadata_node: Node,
) -> list[Path]:
    node_path = _node_path(ctx, node)
    if node.download_kind is not DownloadKind.QUIZ:
        return [node_path]
    return [
        node_path.with_name(f"{node_path.name}.{suffix}")
        for suffix in metadata_node.artifact_hashes
    ]


def _module_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        module_id = int(value)
    except (TypeError, ValueError):
        return None
    return module_id if module_id > 0 else None


def _internal_path_root(ctx: SyncContext) -> InternalPathRoot:
    return ctx.internal_path_root


def _course_cache_path(
    ctx: SyncContext,
    course_node: Node,
    internal_root: InternalPathRoot,
) -> Path:
    identity = _cache_identity(ctx, course_node)
    site_key = hashlib.sha256(str(identity["site"]).encode("utf-8")).hexdigest()
    return internal_root.path(
        COURSE_CACHE_DIRECTORY,
        site_key,
        str(identity["user_id"]),
        str(identity["course_id"]),
        COURSE_CACHE_FILENAME,
    )


def course_cache_path(ctx: SyncContext, course_node: Node) -> Path:
    """Return the stable, account-bound cache path for one Moodle course."""
    cache_path = _course_cache_path(ctx, course_node, _internal_path_root(ctx))
    return with_windows_extended_length_prefix(cache_path)


def _cache_identity(ctx: SyncContext, course_node: Node) -> dict[str, Any]:
    account = ctx.require_moodle_account()
    course_id = _module_id(course_node.id)
    if course_id is None:
        raise ValueError("course cache requires a positive Moodle course id")
    return {
        "site": normalized_site(account.tokens.site),
        "user_id": account.user_id,
        "course_id": course_id,
    }


def _read_course_cache_payload(
    cache_path: Path,
    log: logging.Logger,
) -> dict[str, Any] | None:
    payload = read_private_gzip_json(cache_path, "course cache")
    if not isinstance(payload, dict):
        return None
    if payload.get("format") != COURSE_CACHE_FORMAT:
        log.warning("Ignoring unsupported course cache format: %s", cache_path)
        return None
    return payload


def _legacy_course_cache_paths(
    ctx: SyncContext,
    course_node: Node,
    internal_root: InternalPathRoot,
) -> Iterator[Path]:
    sync_directory = internal_root.root
    direct_path = internal_root.path(
        *sanitized_node_path_parts(course_node), COURSE_CACHE_FILENAME
    )
    if direct_path.is_file():
        yield direct_path
    stable_directory = internal_root.path(COURSE_CACHE_DIRECTORY)
    if ctx.legacy_course_cache_paths is None:
        paths_by_course: dict[int, list[Path]] = {}
        for discovered_path in sync_directory.rglob(COURSE_CACHE_FILENAME):
            path = internal_root.require(discovered_path)
            if not path.is_file() or path.is_relative_to(stable_directory):
                continue
            payload = read_private_gzip_json(path, "legacy course cache")
            if (
                not isinstance(payload, dict)
                or set(payload) != {"format", "course"}
                or payload.get("format") != LEGACY_COURSE_CACHE_FORMAT
                or not isinstance(payload.get("course"), dict)
            ):
                continue
            course_id = _module_id(payload["course"].get("id"))
            if course_id is not None:
                paths_by_course.setdefault(course_id, []).append(path)
        ctx.legacy_course_cache_paths = paths_by_course
    course_id = _module_id(course_node.id)
    for cached_path in ctx.legacy_course_cache_paths.get(course_id or -1, []):
        path = internal_root.require(cached_path)
        if path != direct_path and path.is_file():
            yield path


def _node_tree_has_site_url(course_root: Node, site: str) -> bool:
    expected = urllib.parse.urlsplit(site)
    expected_path = expected.path.rstrip("/") + "/"
    pending = [course_root]
    while pending:
        node = pending.pop()
        pending.extend(node.children)
        if not isinstance(node.url, str) or not node.url:
            continue
        actual = urllib.parse.urlsplit(node.url)
        if (
            actual.scheme.lower() == expected.scheme.lower()
            and actual.netloc.lower() == expected.netloc.lower()
            and actual.path.startswith(expected_path)
        ):
            return True
    return False


def _legacy_download_kind(data: dict[str, Any]) -> DownloadKind:
    node_type = data.get("type")
    return (
        LEGACY_DOWNLOAD_KINDS.get(node_type, DownloadKind.DIRECT)
        if isinstance(node_type, str)
        else DownloadKind.DIRECT
    )


def _shared_legacy_node_data(data: dict[str, Any]) -> dict[str, Any] | None:
    node_type = data.get("type")
    download_kind = _legacy_download_kind(data)
    if node_type == "Assignment File" or download_kind == DownloadKind.QUIZ:
        return None

    shared = {key: value for key, value in data.items() if key in LEGACY_NODE_FIELDS}
    shared["download_kind"] = str(download_kind)
    children = data.get("children")
    if isinstance(children, list):
        if all(isinstance(child, dict) for child in children):
            shared["children"] = [
                shared_child
                for child in children
                if (shared_child := _shared_legacy_node_data(child)) is not None
            ]
        else:
            shared["children"] = children
    return shared


def _account_bound_legacy_payload(
    ctx: SyncContext,
    course_node: Node,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        set(payload) != {"format", "course"}
        or payload.get("format") != LEGACY_COURSE_CACHE_FORMAT
    ):
        return None
    identity = _cache_identity(ctx, course_node)
    course_data = payload.get("course")
    if (
        not isinstance(course_data, dict)
        or _module_id(course_data.get("id")) != identity["course_id"]
    ):
        return None
    shared_course = _shared_legacy_node_data(course_data)
    if shared_course is None:
        return None
    try:
        course_root = node_from_cache_data(shared_course)
    except (TypeError, ValueError):
        return None
    if not _node_tree_has_site_url(course_root, str(identity["site"])):
        return None
    return {
        "format": COURSE_CACHE_FORMAT,
        "identity": identity,
        "course": shared_course,
    }


def _migrate_legacy_course_cache(
    ctx: SyncContext,
    course_node: Node,
    cache_path: Path,
    internal_root: InternalPathRoot,
    log: logging.Logger,
) -> dict[str, Any] | None:
    for legacy_path in _legacy_course_cache_paths(ctx, course_node, internal_root):
        payload = read_private_gzip_json(legacy_path, "legacy course cache")
        if not isinstance(payload, dict):
            continue
        migrated = _account_bound_legacy_payload(ctx, course_node, payload)
        if migrated is None:
            continue
        if ctx.config.dry_run:
            log.info("Using legacy course cache for this dry run: %s", legacy_path)
            return migrated
        try:
            safe_cache_path = internal_root.create_parent(cache_path)
            write_private_gzip_json(
                with_windows_extended_length_prefix(safe_cache_path), migrated
            )
        except OSError as error:
            log.warning(
                "Could not move legacy course cache %s to %s: %s",
                legacy_path,
                cache_path,
                error,
            )
            return migrated
        try:
            internal_root.require(legacy_path).unlink()
        except OSError as error:
            log.warning(
                "Moved legacy course cache to %s but could not remove %s: %s",
                cache_path,
                legacy_path,
                error,
            )
        else:
            log.info("Moved legacy course cache %s to %s", legacy_path, cache_path)
        return migrated
    return None


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


def _course_cache_state(
    ctx: SyncContext,
    course_node: Node,
    log: logging.Logger,
    internal_root: InternalPathRoot | None = None,
) -> CourseCacheState:
    if course_node in ctx.course_cache_states:
        return ctx.course_cache_states[course_node]

    internal_root = internal_root or _internal_path_root(ctx)
    raw_cache_path = _course_cache_path(ctx, course_node, internal_root)
    cache_path = with_windows_extended_length_prefix(raw_cache_path)
    cache_exists = cache_path.exists()
    payload = _read_course_cache_payload(cache_path, log) if cache_exists else None
    if not cache_exists:
        payload = _migrate_legacy_course_cache(
            ctx, course_node, raw_cache_path, internal_root, log
        )
    if payload is not None and payload.get("identity") != _cache_identity(
        ctx, course_node
    ):
        log.warning("Ignoring course cache with mismatched identity: %s", cache_path)
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
    )
    if personal_cache_matches and course_id is not None:
        opencast.restore_cached_episodes(
            ctx,
            course_id,
            raw_cache.get(OPENCAST_EPISODES_CACHE_KEY),
        )
        links.restore_cached_resources(
            ctx,
            course_id,
            raw_cache.get(LINKED_RESOURCES_CACHE_KEY),
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


def _course_module_cache_data(
    ctx: SyncContext,
    state: CourseCacheState,
    course_node: Node,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    course_id = _module_id(course_node.id)
    opencast_data = (
        opencast.cached_episodes_data(ctx, course_id) if course_id is not None else None
    )
    linked_resources_data = (
        links.cached_resources_data(
            ctx,
            course_id,
            complete_inventory=state.complete_module_inventory,
        )
        if course_id is not None
        else None
    )
    cached_text = {
        kind: _cached_text_data(state.cached_text[kind], kind)
        for kind in CACHED_TEXT_KINDS
        if state.cached_text[kind]
    }
    if cached_text:
        data[CACHED_TEXT_CACHE_KEY] = cached_text
    if state.assignments or state.quizzes or opencast_data or linked_resources_data:
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
    if opencast_data is not None:
        data[OPENCAST_EPISODES_CACHE_KEY] = opencast_data
    if linked_resources_data is not None:
        data[LINKED_RESOURCES_CACHE_KEY] = linked_resources_data
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
    artifact_hashes = dict(node.artifact_hashes)
    remote_size = node.remote_size
    is_handled = node.is_handled
    is_verified = node.is_verified
    node_paths = _node_artifact_paths(ctx, node, node)
    verified_this_run = any(path in ctx.downloaded_paths for path in node_paths)
    old_node_paths = (
        _node_artifact_paths(ctx, node, old_node) if old_node is not None else []
    )
    # If this file was not actually downloaded this run but a previously
    # downloaded version is still on disk, keep the previously cached version
    # markers. The node may still be marked as handled when download traversal
    # skipped an unchanged existing file; downloaded_paths tells us whether
    # current remote bytes were installed or verified in this run.
    if (
        not verified_this_run
        and old_node is not None
        and old_node.is_verified
        and old_node_paths
        and all(path.exists() for path in old_node_paths)
    ):
        timemodified = old_node.timemodified
        etag = old_node.etag
        etag_kind = old_node.etag_kind
        content_hash = old_node.content_hash
        artifact_hashes = dict(old_node.artifact_hashes)
        remote_size = remote_size if remote_size is not None else old_node.remote_size
        is_handled = True
        is_verified = True
    elif is_handled and not is_verified:
        timemodified = None
        etag = None
        etag_kind = None
        content_hash = None
        artifact_hashes = {}
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
        "artifact_hashes": artifact_hashes,
        "remote_size": remote_size,
        "name_clash_id": node.name_clash_id,
        "download_status": str(
            DownloadStatus.HANDLED
            if is_verified
            else DownloadStatus.SKIPPED
            if is_handled
            else DownloadStatus.PENDING
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
        artifact_hashes=data.get("artifact_hashes"),
        remote_size=data.get("remote_size"),
        name_clash_id=data.get("name_clash_id", NAME_CLASH_ID_UNSET),
        download_status=data.get("download_status"),
        download_kind=data.get("download_kind"),
    )
    node.children = [node_from_cache_data(child, node) for child in children]
    return node


def get_course_node(node: Node) -> Node:
    """Return the enclosing course node for the given node."""
    course_node = node.ancestor(NodeKind.COURSE)
    if course_node is None:
        raise ValueError("Node is not part of a course subtree")
    return course_node


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
    except ValueError:
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
    """Persist account-bound course caches under the stable cache directory."""
    if not ctx.root_node:
        return

    internal_root = _internal_path_root(ctx)
    for semester_node in ctx.root_node.children:
        if semester_node.type != NodeKind.SEMESTER:
            continue
        for course_node in semester_node.children:
            if course_node.type != NodeKind.COURSE:
                continue
            state = _course_cache_state(ctx, course_node, log, internal_root)
            if course_node.id in ctx.incomplete_course_ids:
                continue
            raw_cache_path = _course_cache_path(ctx, course_node, internal_root)
            internal_root.create_parent(raw_cache_path)
            cache_path = with_windows_extended_length_prefix(raw_cache_path)
            payload: dict[str, Any] = {
                "format": COURSE_CACHE_FORMAT,
                "identity": _cache_identity(ctx, course_node),
                "course": node_to_cache_data(ctx, course_node, state.course_root),
            }
            module_cache = _course_module_cache_data(ctx, state, course_node)
            if module_cache:
                payload[MODULE_CACHE_KEY] = module_cache
            write_private_gzip_json(cache_path, payload)
            state.course_root = node_from_cache_data(payload["course"])
