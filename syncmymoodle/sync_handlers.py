import html
import io
import logging
import re
import tempfile
import urllib.parse
import zipfile
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Callable, cast

import requests

from syncmymoodle import course_cache, filters, moodle_files
from syncmymoodle import links as links_api
from syncmymoodle import moodle as moodle_api
from syncmymoodle import opencast as opencast_api
from syncmymoodle.constants import HTTP_TIMEOUT_SECONDS, MOODLE_URL
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import (
    HTML_CONTENT_TYPES,
    content_length,
    content_type_without_parameters,
    copy_capped_body,
    parse_html,
    read_capped_body,
    safe_request_error,
)
from syncmymoodle.node import DownloadKind, Node

logger = logging.getLogger(__name__)
H5P_PACKAGE_MAX_BYTES = 2 * 1024**3
H5P_PACKAGE_MEMORY_BYTES = 16 * 1024**2
H5P_CONTENT_MAX_BYTES = 10 * 1024 * 1024
H5P_RANGE_CHUNK_BYTES = 64 * 1024
H5P_RANGE_MAX_BYTES = 32 * 1024**2
H5P_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)", re.IGNORECASE)
QUIZ_IMMEDIATE_REVIEW_SECONDS = 2 * 60


@dataclass
class ModuleContext:
    ctx: SyncContext
    course_id: Any
    course_node: Node
    section_node: Node
    assignments_by_cmid: Any
    folders_by_coursemodule: Any
    course_updates: moodle_api.CourseUpdates | None = None
    log: logging.Logger = logger

    def status(self, message: str) -> None:
        self.ctx.output.sync_progress.module_status(message)


@dataclass(frozen=True)
class QuizCacheTiming:
    timeclose: int
    refresh_after: int | None


@dataclass(frozen=True)
class _PageScanContent:
    text: str
    base_url: str
    cache_module_id: int | None
    marker: str | None
    cacheable: bool


@dataclass(frozen=True)
class _OpencastLtiLaunch:
    endpoint: str
    parameters: dict[str, Any]
    title: Any
    series_id: Any
    episode_id: Any


Handler = Callable[[ModuleContext, dict[str, Any]], None]

# Handlers register themselves here via @register_handler and run, per module,
# in registration (definition) order.
MODULE_HANDLERS: list[Handler] = []


def register_handler(handler: Handler) -> Handler:
    """Register a sync module handler so ``handle_module`` dispatches to it."""
    MODULE_HANDLERS.append(handler)
    return handler


def _content_marker(
    metadata: dict[str, Any],
    file_metadata: dict[str, Any],
    *,
    url: str | None = None,
) -> str | None:
    content_hash = metadata.get("contenthash")
    if not isinstance(content_hash, str) or not content_hash:
        content_hash = file_metadata.get("contenthash")
    if isinstance(content_hash, str) and content_hash:
        marker = f"contenthash:{content_hash}"
    else:
        modified = file_metadata.get("timemodified", metadata.get("timemodified"))
        if not isinstance(modified, int) or isinstance(modified, bool) or modified < 0:
            return None
        size = file_metadata.get("filesize")
        size_marker = (
            str(size)
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0
            else "unknown"
        )
        marker = f"timemodified:{modified}:filesize:{size_marker}"

    return f"{marker}:url:{url}" if url is not None else marker


def _page_response_cacheable(response: Any, requested_url: str) -> bool:
    content_type = content_type_without_parameters(response)
    if content_type and content_type not in HTML_CONTENT_TYPES:
        return False

    requested = urllib.parse.urlsplit(requested_url)
    final = urllib.parse.urlsplit(response.url or requested_url)

    def moodle_file_path(path: str) -> str:
        prefix = "/webservice/pluginfile.php/"
        return (
            f"/pluginfile.php/{path.removeprefix(prefix)}"
            if path.startswith(prefix)
            else path
        )

    return (
        requested.scheme.lower() == final.scheme.lower()
        and requested.netloc.lower() == final.netloc.lower()
        and moodle_file_path(requested.path) == moodle_file_path(final.path)
    )


class _H5PRangeUnavailable(Exception):
    """The package cannot be exposed as a reliable HTTP range stream."""


class _H5PRangeReader(io.BufferedIOBase):
    """Seekable, bounded view of an HTTP resource backed by range requests."""

    def __init__(self, session: Any, url: str, size: int) -> None:
        super().__init__()
        self._session = session
        self._url = url
        self._size = size
        self._position = 0
        self._cache_start = 0
        self._cache = b""
        self._transferred = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed H5P package")
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._position + offset
        elif whence == io.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError(f"unsupported seek mode: {whence}")
        if position < 0:
            raise OSError("negative seek position")
        self._position = position
        return position

    def _request_range(self, start: int, end: int) -> bytes:
        expected_size = end - start + 1
        try:
            with closing(
                self._session.get(
                    self._url,
                    headers={
                        "Accept-Encoding": "identity",
                        "Range": f"bytes={start}-{end}",
                    },
                    stream=True,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
            ) as response:
                if response.status_code != 206:
                    raise _H5PRangeUnavailable
                match = H5P_CONTENT_RANGE_RE.fullmatch(
                    response.headers.get("Content-Range", "").strip()
                )
                if match is None or tuple(map(int, match.groups())) != (
                    start,
                    end,
                    self._size,
                ):
                    raise _H5PRangeUnavailable
                declared_size = content_length(response)
                if declared_size is not None and declared_size != expected_size:
                    raise _H5PRangeUnavailable
                body = read_capped_body(response, expected_size)
        except requests.RequestException as error:
            raise _H5PRangeUnavailable from error
        if body is None or len(body) != expected_size:
            raise _H5PRangeUnavailable
        self._transferred += len(body)
        return body

    def _load_range(self, start: int, minimum_size: int) -> None:
        budget = H5P_RANGE_MAX_BYTES - self._transferred
        if minimum_size > budget:
            raise _H5PRangeUnavailable
        request_size = min(
            self._size - start,
            max(minimum_size, min(H5P_RANGE_CHUNK_BYTES, budget)),
        )
        self._cache_start = start
        self._cache = self._request_range(start, start + request_size - 1)

    def read(self, size: int | None = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed H5P package")
        if size == 0 or self._position >= self._size:
            return b""
        remaining = (
            self._size - self._position
            if size is None or size < 0
            else min(size, self._size - self._position)
        )
        parts: list[bytes] = []
        while remaining:
            cache_offset = self._position - self._cache_start
            if not 0 <= cache_offset < len(self._cache):
                self._load_range(self._position, remaining)
                cache_offset = 0
            available = min(remaining, len(self._cache) - cache_offset)
            parts.append(self._cache[cache_offset : cache_offset + available])
            self._position += available
            remaining -= available
        return b"".join(parts)


def _read_h5p_archive_content(
    archive: zipfile.ZipFile,
    module_id: Any,
    log: logging.Logger,
) -> str | None:
    info = archive.getinfo("content/content.json")
    if info.file_size > H5P_CONTENT_MAX_BYTES:
        log.warning("H5P content for module %s is too large", module_id)
        return None
    with archive.open(info) as content_file:
        content = content_file.read(H5P_CONTENT_MAX_BYTES + 1)
    if len(content) > H5P_CONTENT_MAX_BYTES:
        log.warning("H5P content for module %s is too large", module_id)
        return None
    return content.decode("utf-8")


def _read_h5p_content_by_range(
    session: Any,
    package_url: str,
    package_size: int,
    module_id: Any,
    log: logging.Logger,
) -> str | None:
    with _H5PRangeReader(session, package_url, package_size) as package_file:
        with zipfile.ZipFile(package_file) as archive:
            return _read_h5p_archive_content(archive, module_id, log)


def _read_full_h5p_content(
    session: Any,
    package_url: str,
    module_id: Any,
    log: logging.Logger,
) -> str | None:
    with closing(
        session.get(
            package_url,
            stream=True,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    ) as response:
        if not 200 <= response.status_code < 300:
            log.warning(
                "H5P package for module %s returned status %s",
                module_id,
                response.status_code,
            )
            return None
        declared_size = content_length(response)
        if declared_size is not None and declared_size > H5P_PACKAGE_MAX_BYTES:
            log.warning("H5P package for module %s is too large", module_id)
            return None
        with tempfile.SpooledTemporaryFile(
            max_size=H5P_PACKAGE_MEMORY_BYTES,
            mode="w+b",
        ) as package_file:
            if not copy_capped_body(response, package_file, H5P_PACKAGE_MAX_BYTES):
                log.warning("H5P package for module %s is too large", module_id)
                return None
            package_file.seek(0)
            with zipfile.ZipFile(package_file) as archive:
                return _read_h5p_archive_content(archive, module_id, log)


def _read_h5p_content(
    session: Any,
    package_url: str,
    module_id: Any,
    log: logging.Logger,
    package_size: Any = None,
) -> str | None:
    try:
        known_size = (
            package_size
            if isinstance(package_size, int)
            and not isinstance(package_size, bool)
            and package_size > 0
            else None
        )
        if known_size is not None and known_size > H5P_PACKAGE_MAX_BYTES:
            log.warning("H5P package for module %s is too large", module_id)
            return None
        if known_size is not None:
            try:
                return _read_h5p_content_by_range(
                    session,
                    package_url,
                    known_size,
                    module_id,
                    log,
                )
            except _H5PRangeUnavailable:
                pass
        return _read_full_h5p_content(session, package_url, module_id, log)
    except (
        KeyError,
        NotImplementedError,
        OSError,
        RuntimeError,
        UnicodeDecodeError,
        requests.RequestException,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        log.warning("Could not inspect H5P package for module %s", module_id)
        return None


def module_instance(
    ctx: SyncContext,
    module: dict[str, Any],
    course_id: Any,
    cache: dict[int, dict[str, Any]],
    fetch: Callable[[Any, str, int], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    module_id = int(module["id"])
    if module_id not in cache:
        account = ctx.require_moodle_account()
        for item in fetch(ctx.require_session(), account.wstoken, int(course_id)):
            course_module = item.get("coursemodule")
            if isinstance(course_module, int):
                cache[course_module] = item
    return cache.get(module_id)


def _assignment_submission_files(
    module_context: ModuleContext,
    module_id: int,
    assignment_id: Any,
    *,
    allow_cache: bool,
) -> list[dict[str, Any]] | None:
    ctx = module_context.ctx
    cached = course_cache.get_assignment_cache_entry(
        ctx, module_context.course_node, module_id, module_context.log
    )
    if (
        allow_cache
        and cached is not None
        and module_context.course_updates is not None
        and module_context.course_updates.confirms_unchanged(module_id, cached.since)
    ):
        module_context.status("scanning cached assignment submissions")
        return cached.files

    account = ctx.require_moodle_account()
    module_context.status("loading assignment submissions")
    fetched = moodle_api.get_assignment_submission_files(
        ctx.require_session(),
        account.wstoken,
        account.user_id,
        assignment_id,
    )
    if fetched is None:
        return None
    return [item for item in fetched if isinstance(item, dict)]


def _add_assignment_file_nodes(
    ctx: SyncContext,
    assignment_node: Node,
    files: list[Any],
) -> None:
    for item in files:
        if not isinstance(item, dict):
            continue
        if filters.should_skip_url(ctx, item.get("fileurl"), "assignment file"):
            continue
        moodle_files.add_moodle_file_node(
            assignment_node,
            item.get("filepath", "/"),
            item["filename"],
            item["fileurl"],
            "Assignment File",
            item["fileurl"],
            timemodified=item.get("timemodified"),
            remote_size=item.get("filesize"),
        )


@register_handler
def handle_assignment_module(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    ctx = module_context.ctx
    section_node = module_context.section_node
    course_id = module_context.course_id
    assignments_by_cmid = module_context.assignments_by_cmid

    # Get Assignments
    if module["modname"] == "assign" and ctx.config.module_assignment:
        ass = assignments_by_cmid.get(module["id"])
        if not ass:
            return
        assignment_id = ass["id"]
        module_id = module["id"]
        if not isinstance(module_id, int) or isinstance(module_id, bool):
            return
        assignment_name = module["name"]
        assignment_node = section_node.add_child(
            assignment_name, assignment_id, "Assignment"
        )
        if assignment_node is None:
            return

        assignment_intro = ass.get("intro")
        if assignment_intro:
            module_context.status("scanning assignment links")
            links_api.scan_for_links(
                ctx,
                assignment_intro,
                assignment_node,
                course_id,
                module_title=assignment_name,
            )

        # Moodle's update callback checks the current user's submission row,
        # while a team submission can be changed through the shared group row.
        cache_allowed = not bool(ass.get("teamsubmission"))
        submission_files = _assignment_submission_files(
            module_context,
            module_id,
            assignment_id,
            allow_cache=cache_allowed,
        )
        if submission_files is None:
            return

        intro_attachments = ass.get("introattachments") or []
        _add_assignment_file_nodes(
            ctx, assignment_node, [*intro_attachments, *submission_files]
        )
        if cache_allowed:
            course_cache.store_assignment_cache_entry(
                ctx,
                module_context.course_node,
                module_id,
                submission_files,
                module_context.log,
            )
        else:
            course_cache.discard_assignment_cache_entry(
                ctx,
                module_context.course_node,
                module_id,
                module_context.log,
            )


@register_handler
def handle_resource_like_module(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    ctx = module_context.ctx
    section_node = module_context.section_node
    course_id = module_context.course_id

    # Get Resources or URLs
    if module["modname"] not in [
        "resource",
        "url",
        "book",
        "page",
        "pdfannotator",
    ]:
        return
    if module["modname"] == "resource" and not ctx.config.module_resource:
        return
    for c in module.get("contents", []):
        file_url = c.get("fileurl")
        if not file_url:
            continue
        if filters.should_skip_url(ctx, file_url, "resource link"):
            continue
        if moodle_files.is_direct_moodle_file_content(module, c):
            moodle_files.add_moodle_content_file_node(section_node, c)
        elif not (module["modname"] == "page" and c.get("filename") == "index.html"):
            module_context.status("checking linked resource")
            links_api.scan_for_links(
                ctx,
                file_url,
                section_node,
                course_id,
                single=True,
                module_title=module["name"],
            )


@register_handler
def handle_folder_module(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    ctx = module_context.ctx
    section_node = module_context.section_node
    course_id = module_context.course_id
    folders_by_coursemodule = module_context.folders_by_coursemodule

    # Get Folders
    if module["modname"] == "folder" and ctx.config.module_folder:
        folder_node = section_node.add_child(module["name"], module["id"], "Folder")
        if folder_node is None:
            return

        # Scan intro for links
        folder_info = folders_by_coursemodule.get(module["id"])
        if folder_info and folder_info.get("intro"):
            module_context.status("scanning folder links")
            links_api.scan_for_links(ctx, folder_info["intro"], folder_node, course_id)

        for c in module.get("contents", []):
            if filters.should_skip_url(ctx, c.get("fileurl"), "folder file"):
                continue
            moodle_files.add_moodle_file_node(
                folder_node,
                c.get("filepath", "/"),
                c["filename"],
                c["fileurl"],
                "Folder File",
                c["fileurl"],
                timemodified=c.get("timemodified"),
                remote_size=c.get("filesize"),
            )


@register_handler
def handle_embedded_link_module(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    if module["modname"] not in {"page", "label", "h5pactivity"}:
        return
    if not module_context.ctx.config.follow_links:
        return
    if module["modname"] == "page":
        _handle_page_links(module_context, module)
    elif module["modname"] == "h5pactivity":
        _handle_h5p_links(module_context, module)
    else:
        module_context.status("scanning embedded links")
        links_api.scan_for_links(
            module_context.ctx,
            module.get("description", ""),
            module_context.section_node,
            module_context.course_id,
            module_title=module["name"],
        )


def _strict_module_id(module: dict[str, Any]) -> int | None:
    module_id = module["id"]
    return (
        module_id
        if isinstance(module_id, int) and not isinstance(module_id, bool)
        else None
    )


def _page_location(
    module: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    index_content = next(
        (
            content
            for content in module.get("contents") or []
            if content.get("filename") == "index.html"
            and isinstance(content.get("fileurl"), str)
            and content["fileurl"]
        ),
        None,
    )
    html_url = (
        index_content["fileurl"]
        if index_content is not None
        else module.get("url") or f"{MOODLE_URL}mod/page/view.php?id={module['id']}"
    )
    return index_content, html_url


def _cached_text(
    module_context: ModuleContext,
    kind: str,
    module_id: int | None,
    marker: str | None,
) -> course_cache.CachedTextEntry | None:
    if module_id is None or marker is None:
        return None
    return course_cache.get_cached_text(
        module_context.ctx,
        module_context.course_node,
        kind,
        module_id,
        marker,
        module_context.log,
    )


def _fetch_page(
    module_context: ModuleContext,
    module_id: Any,
    html_url: str,
) -> requests.Response | None:
    module_context.status("fetching page")
    try:
        response = module_context.ctx.require_session().get(
            html_url,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        module_context.log.warning(
            "Failed to fetch page module %s: %s",
            module_id,
            safe_request_error(error),
        )
        module_context.ctx.stats.failed += 1
        return None
    if 200 <= response.status_code < 300:
        return response
    module_context.log.warning(
        "Page module %s returned status %s",
        module_id,
        response.status_code,
    )
    module_context.ctx.stats.failed += 1
    return None


def _page_scan_content(
    module_context: ModuleContext,
    module: dict[str, Any],
    index_content: dict[str, Any] | None,
    html_url: str,
) -> _PageScanContent | None:
    cache_module_id = _strict_module_id(module)
    marker = (
        _content_marker(
            index_content,
            index_content,
            url=index_content["fileurl"],
        )
        if index_content is not None
        else None
    )
    cached = _cached_text(
        module_context,
        course_cache.PAGE_CONTENT_KIND,
        cache_module_id,
        marker,
    )
    if cached is not None:
        module_context.status("scanning cached page")
        return _PageScanContent(
            cached.content,
            cached.base_url or html_url,
            cache_module_id,
            marker,
            True,
        )
    response = _fetch_page(module_context, module["id"], html_url)
    if response is None:
        return None
    return _PageScanContent(
        response.text,
        response.url or html_url,
        cache_module_id,
        marker,
        _page_response_cacheable(response, html_url),
    )


def _scan_page_opencast(
    module_context: ModuleContext,
    module: dict[str, Any],
    content: _PageScanContent,
) -> None:
    for iframe in parse_html(content.text).find_all("iframe"):
        iframe_src_value = iframe.get("src")
        if not iframe_src_value:
            continue
        iframe_src = urllib.parse.urljoin(
            content.base_url,
            cast(str, iframe_src_value),
        )
        video_id = opencast_api.extract_episode_id(iframe_src)
        if not video_id:
            continue
        opencast_api.add_episode_nodes(
            module_context.ctx,
            module_context.section_node,
            module["name"],
            video_id,
            module_context.log,
            course_id=module_context.course_id,
        )


def _store_page_content(
    module_context: ModuleContext,
    content: _PageScanContent,
) -> None:
    if (
        content.cache_module_id is None
        or content.marker is None
        or not content.cacheable
    ):
        return
    course_cache.store_cached_text(
        module_context.ctx,
        module_context.course_node,
        course_cache.PAGE_CONTENT_KIND,
        content.cache_module_id,
        content.marker,
        content.text,
        content.base_url,
        module_context.log,
    )


def _handle_page_links(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    index_content, html_url = _page_location(module)
    opencast_enabled = module_context.ctx.config.link_source_enabled("opencast")
    scan_page_links = not filters.should_skip_url(
        module_context.ctx, html_url, "page link"
    )
    if not opencast_enabled and not scan_page_links:
        return
    content = _page_scan_content(module_context, module, index_content, html_url)
    if content is None:
        return
    if opencast_enabled:
        _scan_page_opencast(module_context, module, content)
    if scan_page_links:
        module_context.status("scanning page links")
        links_api.scan_html_text_for_links(
            module_context.ctx,
            content.text,
            content.base_url,
            module_context.section_node,
            module_context.course_id,
            module_title=module["name"],
        )
    _store_page_content(module_context, content)


def _h5p_package_file(activity: dict[str, Any]) -> dict[str, Any] | None:
    package_files = activity.get("package")
    if isinstance(package_files, dict):
        package_files = [package_files]
    return next(
        (
            item
            for item in package_files or []
            if isinstance(item, dict) and isinstance(item.get("fileurl"), str)
        ),
        None,
    )


def _handle_h5p_links(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    ctx = module_context.ctx
    module_context.status("loading H5P activity")
    activity = module_instance(
        ctx,
        module,
        module_context.course_id,
        ctx.h5p_activity_cache,
        moodle_api.get_h5pactivities_by_course,
    )
    if not isinstance(activity, dict):
        return
    package_file = _h5p_package_file(activity)
    if package_file is None:
        return
    package_url = package_file["fileurl"]
    assert isinstance(package_url, str)
    module_id = _strict_module_id(module)
    marker = _content_marker(activity, package_file)
    cached = _cached_text(
        module_context,
        course_cache.H5P_CONTENT_KIND,
        module_id,
        marker,
    )
    content = cached.content if cached is not None else None
    if content is None:
        module_context.status("downloading H5P package")
        content = _read_h5p_content(
            ctx.require_session(),
            package_url,
            module["id"],
            module_context.log,
            package_file.get("filesize"),
        )
        if content is not None and module_id is not None and marker is not None:
            course_cache.store_cached_text(
                ctx,
                module_context.course_node,
                course_cache.H5P_CONTENT_KIND,
                module_id,
                marker,
                content,
                log=module_context.log,
            )
    if content is None:
        return
    module_context.status(
        "scanning cached H5P content" if cached is not None else "scanning H5P content"
    )
    links_api.scan_for_links(
        ctx,
        content,
        module_context.section_node,
        module_context.course_id,
        module_title=module["name"],
        single=False,
    )


def _opencast_lti_launch(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> _OpencastLtiLaunch | None:
    ctx = module_context.ctx
    module_context.status("loading Opencast activity")
    instance = module_instance(
        ctx,
        module,
        module_context.course_id,
        ctx.lti_instance_cache,
        moodle_api.get_ltis_by_course,
    )
    tool_id = instance.get("id") if instance else module.get("instance")
    if not isinstance(tool_id, int):
        module_context.log.warning(
            "Opencast: LTI module %s has no tool instance id", module["id"]
        )
        return None
    module_context.status("loading Opencast launch data")
    launch_data = moodle_api.get_lti_launch_data(
        ctx.require_session(), ctx.require_moodle_account().wstoken, tool_id
    )
    if launch_data is None:
        return None
    endpoint = launch_data.get("endpoint")
    if not isinstance(endpoint, str):
        module_context.log.warning(
            "Opencast: LTI module %s has no launch endpoint", module["id"]
        )
        return None
    engage_data = {
        str(item["name"]): item.get("value", "")
        for item in launch_data.get("parameters") or []
        if isinstance(item, dict) and item.get("name")
    }
    return _OpencastLtiLaunch(
        endpoint,
        engage_data,
        engage_data.get("resource_link_title") or module["name"],
        engage_data.get("custom_series"),
        engage_data.get("custom_id"),
    )


def _handle_opencast_series(
    module_context: ModuleContext,
    module: dict[str, Any],
    launch: _OpencastLtiLaunch,
) -> None:
    ctx = module_context.ctx
    if not opencast_api.course_is_authorized(
        ctx,
        module_context.course_id,
        launch.endpoint,
    ):
        module_context.status("authorizing Opencast course")
        if not opencast_api.submit_lti_form(
            ctx,
            launch.parameters,
            f"LTI series module {module['id']}",
            module_context.log,
            endpoint=launch.endpoint,
            course_id=module_context.course_id,
        ):
            return
    episodes = opencast_api.list_series_episodes(
        ctx,
        launch.series_id,
        module_context.log,
        module_context.course_id,
    )
    if episodes is None:
        return
    series_node = cast(
        Node,
        module_context.course_node.add_child(launch.title, launch.series_id, "Section"),
    )
    for index, (episode_id, episode_title) in enumerate(episodes, start=1):
        module_context.status(f"resolving Opencast episode {index}/{len(episodes)}")
        opencast_api.add_episode_nodes(
            ctx,
            series_node,
            episode_title,
            episode_id,
            module_context.log,
            course_id=module_context.course_id,
        )


def _handle_opencast_episode(
    module_context: ModuleContext,
    module: dict[str, Any],
    launch: _OpencastLtiLaunch,
) -> None:
    if not launch.episode_id:
        module_context.log.info(
            "Opencast LTI module %s has neither custom_id nor custom_series",
            module["id"],
        )
        return
    if not opencast_api.course_is_authorized(
        module_context.ctx,
        module_context.course_id,
        launch.endpoint,
    ):
        module_context.status("authorizing Opencast course")
        if not opencast_api.submit_lti_form(
            module_context.ctx,
            launch.parameters,
            f"LTI module {module['id']}",
            module_context.log,
            endpoint=launch.endpoint,
            course_id=module_context.course_id,
        ):
            return
    opencast_api.add_episode_nodes(
        module_context.ctx,
        module_context.section_node,
        launch.title,
        launch.episode_id,
        module_context.log,
        course_id=module_context.course_id,
    )


@register_handler
def handle_opencast_lti_module(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    ctx = module_context.ctx
    if module["modname"] != "lti" or not ctx.config.link_source_enabled("opencast"):
        return
    launch = _opencast_lti_launch(module_context, module)
    if launch is None:
        return
    if launch.series_id:
        _handle_opencast_series(module_context, module, launch)
    else:
        _handle_opencast_episode(module_context, module, launch)


def _quiz_review_html(name: str, review: dict[str, Any]) -> str:
    parts: list[str] = []
    grade = review.get("grade")
    if grade not in (None, ""):
        parts.append(
            '<section class="quiz-grade"><h2>Grade</h2><p>'
            f"{html.escape(str(grade))}</p></section>"
        )
    parts.extend(
        str(question.get("html") or "")
        for question in review.get("questions") or []
        if isinstance(question, dict)
    )
    for item in review.get("additionaldata") or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if title not in (None, ""):
            parts.append(f"<h2>{html.escape(str(title))}</h2>")
        parts.append(str(item.get("content") or ""))
    return (
        "<!doctype html><html><head><title>"
        f"{html.escape(name)}</title></head><body>{''.join(parts)}</body></html>"
    )


def _quiz_cache_timing(
    attempts: list[dict[str, Any]],
    quiz: dict[str, Any] | None,
    server_time: int | None,
) -> QuizCacheTiming | None:
    """Return cache timing metadata, or ``None`` when it cannot be trusted."""
    if quiz is None or server_time is None:
        return None
    timeclose = quiz.get("timeclose")
    if not isinstance(timeclose, int) or isinstance(timeclose, bool) or timeclose < 0:
        return None

    boundaries = [timeclose] if timeclose > server_time else []
    for attempt in attempts:
        attempt_id = attempt.get("id")
        timefinish = attempt.get("timefinish")
        if (
            not isinstance(attempt_id, int)
            or isinstance(attempt_id, bool)
            or attempt_id <= 0
            or not isinstance(timefinish, int)
            or isinstance(timefinish, bool)
            or timefinish <= 0
        ):
            return None
        immediate_review_end = timefinish + QUIZ_IMMEDIATE_REVIEW_SECONDS
        if immediate_review_end > server_time:
            boundaries.append(immediate_review_end)
    return QuizCacheTiming(timeclose, min(boundaries, default=None))


def _cached_quiz_data(
    module_context: ModuleContext,
    module_id: int,
    quiz: dict[str, Any] | None,
) -> (
    tuple[
        list[dict[str, Any]],
        dict[int, dict[str, Any]],
        QuizCacheTiming,
    ]
    | None
):
    cached = course_cache.get_quiz_cache_entry(
        module_context.ctx,
        module_context.course_node,
        module_id,
        module_context.log,
    )
    timing = _quiz_cache_timing(
        cached.attempts if cached is not None else [],
        quiz,
        module_context.ctx.moodle_server_time,
    )
    if (
        cached is None
        or timing is None
        or timing.timeclose != cached.timeclose
        or timing.refresh_after != cached.refresh_after
        or module_context.course_updates is None
        or not module_context.course_updates.confirms_unchanged(module_id, cached.since)
    ):
        return None
    valid_attempt_ids = {
        attempt_id
        for attempt in cached.attempts
        if isinstance((attempt_id := attempt.get("id")), int)
        and not isinstance(attempt_id, bool)
    }
    if not valid_attempt_ids.issubset(cached.reviews):
        return None
    return cached.attempts, cached.reviews, timing


def _load_quiz_data(
    module_context: ModuleContext,
    quiz_id: int,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], bool] | None:
    ctx = module_context.ctx
    module_context.status("loading quiz attempts")
    attempts = moodle_api.get_quiz_attempts(
        ctx.require_session(), ctx.require_moodle_account().wstoken, quiz_id
    )
    if attempts is None:
        return None

    reviews: dict[int, dict[str, Any]] = {}
    complete = True
    for index, attempt in enumerate(attempts, 1):
        attempt_id = attempt.get("id")
        if (
            not isinstance(attempt_id, int)
            or isinstance(attempt_id, bool)
            or attempt_id <= 0
        ):
            complete = False
            continue
        module_context.status(f"loading quiz attempt {index}/{len(attempts)}")
        review = moodle_api.get_quiz_attempt_review(
            ctx.require_session(),
            ctx.require_moodle_account().wstoken,
            attempt_id,
        )
        if review is None:
            complete = False
        else:
            reviews[attempt_id] = review
    return attempts, reviews, complete


def _add_quiz_nodes(
    ctx: SyncContext,
    section_node: Node,
    module_name: str,
    attempts: list[dict[str, Any]],
    reviews: dict[int, dict[str, Any]],
) -> None:
    for index, attempt in enumerate(attempts, 1):
        attempt_id = attempt.get("id")
        if not isinstance(attempt_id, int) or isinstance(attempt_id, bool):
            continue
        review = reviews.get(attempt_id)
        if review is None:
            continue
        name = f"{module_name}, Versuch {index}"
        review_url = f"{MOODLE_URL}mod/quiz/review.php?attempt={attempt_id}"
        ctx.quiz_review_cache[review_url] = _quiz_review_html(name, review)
        section_node.add_child(
            name,
            attempt_id,
            "Quiz",
            url=review_url,
            download_kind=DownloadKind.QUIZ,
        )


@register_handler
def handle_quiz_module(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    ctx = module_context.ctx
    section_node = module_context.section_node

    # Integration for Quizzes
    if module["modname"] != "quiz" or ctx.config.quiz_mode == "off":
        return

    module_id = module.get("id")
    if not isinstance(module_id, int) or isinstance(module_id, bool):
        return

    # Effective quiz closing times include user/group overrides and determine
    # when Moodle changes which review fields are visible.
    module_context.status("loading quiz activity")
    instance = module_instance(
        ctx,
        module,
        module_context.course_id,
        ctx.quiz_instance_cache,
        moodle_api.get_quizzes_by_course,
    )
    quiz_id = instance.get("id") if instance else module.get("instance")
    if not isinstance(quiz_id, int) or isinstance(quiz_id, bool):
        return
    cached_data = _cached_quiz_data(module_context, module_id, instance)
    timing: QuizCacheTiming | None
    if cached_data is not None:
        module_context.status("scanning cached quiz attempts")
        attempts, reviews, timing = cached_data
        complete = True
    else:
        loaded = _load_quiz_data(module_context, quiz_id)
        if loaded is None:
            return
        attempts, reviews, complete = loaded
        timing = _quiz_cache_timing(
            attempts,
            instance,
            ctx.moodle_server_time,
        )

    _add_quiz_nodes(ctx, section_node, module["name"], attempts, reviews)
    if complete and timing is not None:
        course_cache.store_quiz_cache_entry(
            ctx,
            module_context.course_node,
            module_id,
            attempts,
            reviews,
            timing.timeclose,
            timing.refresh_after,
            module_context.log,
        )
    else:
        course_cache.discard_quiz_cache_entry(
            ctx,
            module_context.course_node,
            module_id,
            module_context.log,
        )


def handle_module(module_context: ModuleContext, module: dict[str, Any]) -> None:
    for handler in MODULE_HANDLERS:
        handler(module_context, module)
