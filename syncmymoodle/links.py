import logging
import math
import time
import urllib.parse
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from typing import Any, cast

import requests
from yt_dlp.extractor.youtube import YoutubeIE  # type: ignore[import-untyped]

from syncmymoodle import emedia as emedia_api
from syncmymoodle import filters, moodle_files
from syncmymoodle import opencast as opencast_api
from syncmymoodle import sciebo as sciebo_api
from syncmymoodle.constants import (
    EMEDIA_LINK_RE,
    HTTP_TIMEOUT_SECONDS,
    LINKED_PAGE_MAX_BYTES,
    OPENCAST_LINK_RE,
    SCIEBO_LINK_RE,
    YOUTUBE_LINK_RE,
    YOUTUBE_WATCH_URL,
)
from syncmymoodle.context import LinkedResourceCacheEntry, SyncContext
from syncmymoodle.http_utils import (
    HTML_CONTENT_TYPES,
    HttpFailureKind,
    classify_http_failure,
    classify_request_failure,
    content_length,
    content_type_without_parameters,
    filename_from_url,
    normalized_http_origin,
    parse_html,
    read_capped_body,
    record_service_failure,
    redact_url_secrets,
    request_following_safe_redirects,
    safe_request_error,
)
from syncmymoodle.node import DownloadKind, Node, RemoteMarkerKind

logger = logging.getLogger(__name__)
LINKED_RESOURCES_CACHE_FORMAT = "syncmymoodle.linked-resources.v1"


@dataclass(frozen=True)
class LinkedResourceResolution:
    """One reusable generic-link lookup result and its inventory semantics."""

    resource: LinkedResourceCacheEntry | None
    cacheable: bool = True
    failure: str | None = None
    inventory_incomplete: bool = False


@dataclass(frozen=True)
class _HeadResolution:
    resource: LinkedResourceCacheEntry | None
    final_url: str
    origin: str | None
    cacheable: bool = True
    failure_status: int | None = None


def _response_header(response: Any, name: str) -> str | None:
    for header_name, value in response.headers.items():
        if str(header_name).lower() == name.lower() and value:
            return str(value)
    return None


def _cache_policy(response: Any) -> tuple[bool, float | None]:
    directives: dict[str, str | None] = {}
    for directive in (_response_header(response, "Cache-Control") or "").split(","):
        name, separator, value = directive.strip().partition("=")
        name = name.strip().lower()
        if name:
            directives[name] = value.strip().strip('"') if separator else None
    if "no-store" in directives:
        return False, None
    if "no-cache" in directives or "max-age" not in directives:
        return True, None
    try:
        max_age = int(directives["max-age"] or "")
        age = max(0, int(_response_header(response, "Age") or "0"))
    except ValueError:
        return True, None
    if max_age < 0:
        return True, None
    return True, time.time() + max(0, max_age - age)


def _response_cache_entry(
    response: Any,
    html: str | None,
) -> tuple[LinkedResourceCacheEntry, bool]:
    cacheable, fresh_until = _cache_policy(response)
    content_type = content_type_without_parameters(response)
    if not content_type:
        content_type = "text/html" if html is not None else "application/octet-stream"
    return (
        LinkedResourceCacheEntry(
            final_url=str(response.url),
            content_type=content_type,
            html=html,
            etag=_response_header(response, "ETag"),
            last_modified=_response_header(response, "Last-Modified"),
            fresh_until=fresh_until,
            remote_size=content_length(response),
        ),
        cacheable,
    )


def _revalidated_cache_entry(
    cached: LinkedResourceCacheEntry,
    response: Any,
) -> tuple[LinkedResourceCacheEntry, bool]:
    cacheable, fresh_until = _cache_policy(response)
    remote_size = content_length(response)
    return (
        LinkedResourceCacheEntry(
            final_url=cached.final_url,
            content_type=cached.content_type,
            html=cached.html,
            etag=_response_header(response, "ETag") or cached.etag,
            last_modified=(
                _response_header(response, "Last-Modified") or cached.last_modified
            ),
            fresh_until=fresh_until,
            remote_size=(
                remote_size if remote_size is not None else cached.remote_size
            ),
        ),
        cacheable,
    )


def _conditional_headers(cached: LinkedResourceCacheEntry | None) -> dict[str, str]:
    if cached is None:
        return {}
    headers = {}
    if cached.etag:
        headers["If-None-Match"] = cached.etag
    if cached.last_modified:
        headers["If-Modified-Since"] = cached.last_modified
    return headers


def _cached_resource_entries(value: Any) -> dict[str, LinkedResourceCacheEntry]:
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
            or not isinstance(raw_entry, dict)
        ):
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


def restore_cached_resources(ctx: SyncContext, course_id: Any, value: Any) -> None:
    """Restore persisted link metadata into the provider's runtime cache."""
    course_key = str(course_id)
    cached = ctx.linked_resources_by_course.get(course_key)
    restored = _cached_resource_entries(value)
    if cached is None:
        ctx.linked_resources_by_course[course_key] = restored
        return
    for requested_url, entry in restored.items():
        cached.setdefault(requested_url, entry)


def cached_resources_data(
    ctx: SyncContext,
    course_id: Any,
    *,
    complete_inventory: bool,
) -> dict[str, Any] | None:
    """Snapshot the link metadata that remains relevant to one course."""
    course_key = str(course_id)
    entries = ctx.linked_resources_by_course.get(course_key, {})
    if complete_inventory:
        seen_urls = {
            requested_url
            for seen_course, requested_url in ctx.seen_linked_resources
            if seen_course == course_key
        }
        entries = {
            requested_url: entry
            for requested_url, entry in entries.items()
            if requested_url in seen_urls
        }
        ctx.linked_resources_by_course[course_key] = entries
    if not entries:
        return None
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


def _head_linked_resource(
    ctx: SyncContext,
    url: str,
    cached: LinkedResourceCacheEntry | None,
    url_allowed: Callable[[str], bool],
) -> _HeadResolution:
    headers = _conditional_headers(cached)
    conditional = bool(headers)
    while True:
        response = request_following_safe_redirects(
            ctx.require_session(),
            "HEAD",
            url,
            url_allowed,
            headers=headers,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        with closing(response):
            final_url = str(response.url or url)
            request_origin = normalized_http_origin(final_url)
            if (
                response.status_code == 304
                and conditional
                and cached is not None
                and final_url == cached.final_url
            ):
                entry, cacheable = _revalidated_cache_entry(cached, response)
                return _HeadResolution(entry, final_url, request_origin, cacheable)
            if response.status_code == 304 and conditional:
                headers = {}
                conditional = False
                continue
            content_type = content_type_without_parameters(response)
            if (
                200 <= response.status_code < 300
                and content_type
                and content_type not in HTML_CONTENT_TYPES
            ):
                entry, cacheable = _response_cache_entry(response, None)
                return _HeadResolution(entry, final_url, request_origin, cacheable)
            return _HeadResolution(
                None,
                final_url,
                request_origin,
                True,
                (
                    response.status_code
                    if not 200 <= response.status_code < 300
                    else None
                ),
            )


def _linked_page_html(
    response: Any,
    final_url: str,
    log: logging.Logger,
) -> tuple[str | None, bool]:
    content_type = content_type_without_parameters(response)
    if content_type and content_type not in HTML_CONTENT_TYPES:
        return None, True
    declared_size = content_length(response)
    if declared_size is not None and declared_size > LINKED_PAGE_MAX_BYTES:
        log.warning(
            "Skipping linked page %s because its declared size exceeds the "
            "inspection limit",
            redact_url_secrets(final_url),
        )
        return None, False
    body = read_capped_body(response, LINKED_PAGE_MAX_BYTES)
    if body is None:
        log.warning(
            "Skipping linked page %s because it exceeds the inspection limit",
            redact_url_secrets(final_url),
        )
        return None, False
    encoding = getattr(response, "encoding", None) or "utf-8"
    try:
        return body.decode(encoding, errors="replace"), True
    except LookupError:
        return body.decode("utf-8", errors="replace"), True


def _linked_http_failure(
    ctx: SyncContext,
    response_origin: str | None,
    method: str,
    final_url: str,
    status_code: int,
    log: logging.Logger,
) -> LinkedResourceResolution | None:
    failure_kind = classify_http_failure(status_code)
    if failure_kind is None:
        return None
    failure = f"{method} {redact_url_secrets(final_url)} returned HTTP {status_code}"
    if response_origin:
        record_service_failure(
            ctx.service_outages,
            response_origin,
            f"Link origin {response_origin}",
            failure_kind,
            failure,
            log,
        )
    if failure_kind is HttpFailureKind.RESOURCE:
        if 400 <= status_code < 500:
            log.info("Skipping linked resource: %s", failure)
            return LinkedResourceResolution(None, inventory_incomplete=True)
        log.warning("Skipping linked resource: %s", failure)
    return LinkedResourceResolution(None, failure=failure)


def _get_linked_resource(
    ctx: SyncContext,
    url: str,
    cached: LinkedResourceCacheEntry | None,
    url_allowed: Callable[[str], bool],
    request_origin: str | None,
    log: logging.Logger,
) -> LinkedResourceResolution:
    headers = _conditional_headers(cached)
    conditional = bool(headers)
    while True:
        response = request_following_safe_redirects(
            ctx.require_session(),
            "GET",
            url,
            url_allowed,
            headers=headers,
            stream=True,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        with closing(response):
            final_url = str(response.url or url)
            response_origin = normalized_http_origin(final_url) or request_origin
            if request_origin and response_origin != request_origin:
                ctx.service_outages.record_available(request_origin)
            if (
                response.status_code == 304
                and conditional
                and cached is not None
                and final_url == cached.final_url
            ):
                if response_origin:
                    ctx.service_outages.record_available(response_origin)
                entry, cacheable = _revalidated_cache_entry(cached, response)
                return LinkedResourceResolution(entry, cacheable)
            if response.status_code == 304 and conditional:
                headers = {}
                conditional = False
                continue

            failure = _linked_http_failure(
                ctx,
                response_origin,
                "GET",
                final_url,
                response.status_code,
                log,
            )
            if failure is not None:
                return failure

            if response_origin:
                ctx.service_outages.record_available(response_origin)
            html, usable = _linked_page_html(response, final_url, log)
            if not usable:
                return LinkedResourceResolution(None, cacheable=False)
            entry, cacheable = _response_cache_entry(response, html)
            return LinkedResourceResolution(entry, cacheable)


def _fresh_cached_resource(
    ctx: SyncContext,
    cached: LinkedResourceCacheEntry | None,
    course_id: Any,
) -> tuple[bool, LinkedResourceCacheEntry | None]:
    if (
        cached is None
        or cached.fresh_until is None
        or time.time() >= cached.fresh_until
    ):
        return False, None
    if filters.should_skip_url(
        ctx,
        cached.final_url,
        "cached linked resource",
        course_id=course_id,
    ):
        return True, None
    return True, cached


def _handle_link_request_error(
    ctx: SyncContext,
    url: str,
    request_origin: str | None,
    error: requests.RequestException,
    log: logging.Logger,
) -> LinkedResourceResolution:
    reason = (
        f"request for {redact_url_secrets(url)} failed: {safe_request_error(error)}"
    )
    failure_kind = classify_request_failure(error)
    if request_origin:
        record_service_failure(
            ctx.service_outages,
            request_origin,
            f"Link origin {request_origin}",
            failure_kind,
            reason,
            log,
        )
    if failure_kind is not HttpFailureKind.TRANSIENT and not isinstance(
        error, filters.FilteredRequestError
    ):
        log.warning("Skipping link request: %s", reason)
    # A course-specific URL policy rejection must not become a global negative
    # result for a later course with a more permissive policy.
    filtered = isinstance(error, filters.FilteredRequestError)
    return LinkedResourceResolution(
        None,
        cacheable=not filtered,
        failure=None if filtered else reason,
    )


def _head_only_resolution(
    ctx: SyncContext,
    head: _HeadResolution,
    request_origin: str | None,
    log: logging.Logger,
) -> LinkedResourceResolution:
    if head.failure_status is None:
        return LinkedResourceResolution(None)
    failure = _linked_http_failure(
        ctx,
        request_origin,
        "HEAD",
        head.final_url,
        head.failure_status,
        log,
    )
    assert failure is not None
    return failure


def _resolve_linked_resource(
    ctx: SyncContext,
    url: str,
    cached: LinkedResourceCacheEntry | None,
    course_id: Any,
    log: logging.Logger,
) -> LinkedResourceResolution:
    link_origin = normalized_http_origin(url)
    request_origin = link_origin
    use_cached, fresh_resource = _fresh_cached_resource(ctx, cached, course_id)
    if use_cached:
        return LinkedResourceResolution(fresh_resource)
    if link_origin and ctx.service_outages.should_skip(link_origin):
        return LinkedResourceResolution(
            None,
            failure=f"link origin {link_origin} is unavailable",
        )

    def url_allowed(candidate: str) -> bool:
        return filters.require_url_allowed(
            ctx,
            candidate,
            "redirected link",
            course_id=course_id,
        )

    try:
        if cached is not None and cached.html is not None and ctx.config.follow_links:
            ctx.output.sync_progress.module_status("revalidating linked page")
            return _get_linked_resource(
                ctx,
                url,
                cached,
                url_allowed,
                request_origin,
                log,
            )

        ctx.output.sync_progress.module_status("checking linked resource")
        head = _head_linked_resource(
            ctx,
            url,
            cached,
            url_allowed,
        )
        request_origin = head.origin or request_origin
        if head.resource is not None:
            if request_origin:
                ctx.service_outages.record_available(request_origin)
            return LinkedResourceResolution(head.resource, head.cacheable)

        if not ctx.config.follow_links:
            return _head_only_resolution(ctx, head, request_origin, log)
        if request_origin and ctx.service_outages.should_skip(request_origin):
            return LinkedResourceResolution(
                None,
                failure=f"link origin {request_origin} is unavailable",
            )
        ctx.output.sync_progress.module_status("loading linked page")
        return _get_linked_resource(
            ctx,
            head.final_url,
            None,
            url_allowed,
            request_origin,
            log,
        )
    except requests.RequestException as error:
        return _handle_link_request_error(ctx, url, request_origin, error, log)


def _known_provider_link(url: str) -> bool:
    return bool(
        YOUTUBE_LINK_RE.match(url)
        or OPENCAST_LINK_RE.match(url)
        or SCIEBO_LINK_RE.match(url)
        or EMEDIA_LINK_RE.match(url)
    )


def _retain_cached_resource_when_unavailable(
    resolution: LinkedResourceResolution,
    cached: LinkedResourceCacheEntry | None,
) -> LinkedResourceResolution:
    if (
        resolution.inventory_incomplete
        and resolution.resource is None
        and cached is not None
    ):
        return LinkedResourceResolution(
            cached,
            cacheable=resolution.cacheable,
            inventory_incomplete=True,
        )
    return resolution


def _linked_resource_for_course(
    ctx: SyncContext,
    url: str,
    course_id: Any,
    log: logging.Logger,
) -> LinkedResourceCacheEntry | None:
    course_key = str(course_id)
    cache = ctx.linked_resources_by_course.setdefault(course_key, {})
    cached_resource = cache.get(url)
    if cached_resource is not None and filters.should_skip_url(
        ctx,
        cached_resource.final_url,
        "cached linked resource",
        course_id=course_id,
    ):
        return None
    ctx.seen_linked_resources.add((course_key, url))
    if url in ctx.linked_resource_results:
        resolution = ctx.linked_resource_results[url]
        resource = resolution.resource
        if resource is not None and filters.should_skip_url(
            ctx,
            resource.final_url,
            "cached linked resource",
            course_id=course_id,
        ):
            return None
        if resource is not None:
            cache[url] = resource
    else:
        resolution = _retain_cached_resource_when_unavailable(
            _resolve_linked_resource(
                ctx,
                url,
                cached_resource,
                course_id,
                log,
            ),
            cached_resource,
        )
        resource = resolution.resource
        if resolution.cacheable:
            if resource is not None:
                cache[url] = resource
            ctx.linked_resource_results[url] = resolution
        else:
            cache.pop(url, None)
    if resolution.failure is not None:
        ctx.record_course_failure_once(course_id, f"linked-resource:{url}")
    elif resolution.inventory_incomplete:
        ctx.mark_course_inventory_filtered(course_id)
    elif resource is None and cached_resource is not None:
        ctx.mark_course_incomplete(course_id)
    return resource


def _scan_single_link(
    ctx: SyncContext,
    url: str,
    parent_node: Node,
    course_id: Any,
    module_title: Any,
    log: logging.Logger,
) -> bool:
    """Resolve one generic link and return whether it is a direct file."""
    if filters.should_skip_url(ctx, url, "link", course_id=course_id):
        return False
    resource = _linked_resource_for_course(ctx, url, course_id, log)
    if resource is None:
        return False
    if resource.html is None:
        parent_node.add_download_child(
            filename_from_url(resource.final_url),
            None,
            f"Linked file [{resource.content_type}]",
            url=resource.final_url,
            etag=resource.etag,
            etag_kind=(RemoteMarkerKind.OPAQUE if resource.etag else None),
            remote_size=resource.remote_size,
        )
        return True
    if ctx.config.follow_links:
        scan_html_text_for_links(
            ctx,
            resource.html,
            resource.final_url,
            parent_node,
            course_id,
            module_title=module_title,
            log=log,
        )
    return False


def youtube_video_id(link: str) -> str | None:
    try:
        return cast(str, YoutubeIE.extract_id(link))
    except Exception:
        return None


def canonical_youtube_url(video_id: str) -> str:
    return YOUTUBE_WATCH_URL.format(video_id=video_id)


def youtube_video_id_from_node(node: Node) -> str | None:
    if node.download_kind is not DownloadKind.YOUTUBE:
        return None
    node_id = str(node.id or "")
    node_url = str(node.url or "")
    if node_id and node_id != node_url:
        if youtube_video_id(canonical_youtube_url(node_id)) == node_id:
            return node_id
    for value in (node_url, node_id):
        if value and (video_id := youtube_video_id(value)):
            return video_id
    return None


def scan_html_text_for_links(
    ctx: SyncContext,
    html_text: str,
    base_url: str | None,
    parent_node: Node,
    course_id: Any,
    module_title: Any = None,
    log: logging.Logger = logger,
) -> None:
    if "video-js" in html_text and "<source" in html_text.lower():
        soup = parse_html(html_text)
        videojs = soup.select_one(".video-js")
        if videojs:
            videojs = videojs.select_one("source")
            if videojs and videojs.get("src"):
                video_src = cast(str, videojs["src"])
                link = moodle_files.canonicalize_moodle_file_url(
                    urllib.parse.urljoin(str(base_url or ""), video_src)
                )
                if not filters.should_skip_url(
                    ctx,
                    link,
                    "embedded video",
                    course_id=course_id,
                ):
                    parent_node.add_download_child(
                        video_src.split("/")[-1],
                        None,
                        "Embedded videojs",
                        url=link,
                    )

    scan_for_links(
        ctx,
        html_text,
        parent_node,
        course_id,
        module_title=module_title,
        single=False,
        log=log,
    )


def _scan_youtube_links(
    ctx: SyncContext,
    text: str,
    parent_node: Node,
    course_id: Any,
    module_title: Any,
) -> None:
    if not ctx.config.link_source_enabled("youtube"):
        return
    for match in YOUTUBE_LINK_RE.finditer(text):
        link = match.group(1)
        if filters.should_skip_url(
            ctx,
            link,
            "YouTube link",
            course_id=course_id,
        ):
            continue
        video_id = youtube_video_id(link)
        if video_id is None:
            continue
        canonical_url = canonical_youtube_url(video_id)
        parent_node.add_download_child(
            f"Youtube: {module_title or canonical_url}",
            video_id,
            "Youtube",
            url=canonical_url,
            download_kind=DownloadKind.YOUTUBE,
        )


def _scan_opencast_links(
    ctx: SyncContext,
    text: str,
    parent_node: Node,
    course_id: Any,
    module_title: Any,
    log: logging.Logger,
) -> None:
    if not ctx.config.link_source_enabled("opencast"):
        return
    for video_url in OPENCAST_LINK_RE.findall(text):
        if filters.should_skip_url(
            ctx,
            video_url,
            "Opencast link",
            course_id=course_id,
        ):
            continue
        video_id = opencast_api.extract_episode_id(video_url)
        if not video_id:
            log.warning(
                "Opencast: could not extract episode id from url %s",
                redact_url_secrets(video_url),
            )
            continue
        if not opencast_api.add_episode_nodes(
            ctx,
            parent_node,
            module_title,
            video_id,
            log,
            course_id=course_id,
        ):
            ctx.record_course_failure_once(course_id, f"opencast:{video_id}")


def _scan_emedia_links(
    ctx: SyncContext,
    text: str,
    parent_node: Node,
    course_id: Any,
    module_title: Any,
    log: logging.Logger,
) -> None:
    if not ctx.config.link_source_enabled("emedia"):
        return
    for match in EMEDIA_LINK_RE.finditer(text):
        link = match.group(0)
        if filters.should_skip_url(
            ctx,
            link,
            "emedia link",
            course_id=course_id,
        ):
            continue
        ctx.output.sync_progress.module_status("resolving emedia video")
        if not emedia_api.add_video_node(
            ctx,
            parent_node,
            link,
            module_title,
            log,
            course_id=course_id,
        ):
            video_id = emedia_api.extract_video_id(link)
            ctx.record_course_failure_once(course_id, f"emedia:{video_id}")


def scan_for_links(
    ctx: SyncContext,
    text: str,
    parent_node: Node,
    course_id: Any,
    module_title: Any = None,
    single: bool = False,
    log: logging.Logger = logger,
) -> None:
    if single:
        text = moodle_files.canonicalize_moodle_file_url(text)
        if not _known_provider_link(text) and _scan_single_link(
            ctx,
            text,
            parent_node,
            course_id,
            module_title,
            log,
        ):
            return
    if not ctx.config.follow_links:
        return

    _scan_youtube_links(ctx, text, parent_node, course_id, module_title)
    _scan_opencast_links(ctx, text, parent_node, course_id, module_title, log)
    _scan_emedia_links(ctx, text, parent_node, course_id, module_title, log)
    if ctx.config.link_source_enabled("sciebo"):
        sciebo_api.scan_public_shares(
            ctx,
            text,
            parent_node,
            log,
            course_id=course_id,
        )
