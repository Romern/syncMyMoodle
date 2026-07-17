import logging
import time
import urllib.parse
from collections.abc import Callable
from contextlib import closing
from typing import Any, cast

import requests
from yt_dlp.extractor.youtube import YoutubeIE  # type: ignore[import-untyped]

from syncmymoodle import emedia as emedia_api
from syncmymoodle import filters
from syncmymoodle import opencast as opencast_api
from syncmymoodle import sciebo as sciebo_api
from syncmymoodle.constants import (
    EMEDIA_LINK_RE,
    HTTP_TIMEOUT_SECONDS,
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
    record_service_failure,
    redact_url_secrets,
    request_following_safe_redirects,
    safe_request_error,
)
from syncmymoodle.node import DownloadKind, Node, RemoteMarkerKind

logger = logging.getLogger(__name__)


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


def _head_linked_resource(
    ctx: SyncContext,
    url: str,
    cached: LinkedResourceCacheEntry | None,
    url_allowed: Callable[[str], bool],
) -> tuple[LinkedResourceCacheEntry | None, str, str | None, bool]:
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
                return entry, final_url, request_origin, cacheable
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
                return entry, final_url, request_origin, cacheable
            return None, final_url, request_origin, True


def _get_linked_resource(
    ctx: SyncContext,
    url: str,
    cached: LinkedResourceCacheEntry | None,
    url_allowed: Callable[[str], bool],
    request_origin: str | None,
    log: logging.Logger,
) -> tuple[LinkedResourceCacheEntry | None, bool]:
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
                return _revalidated_cache_entry(cached, response)
            if response.status_code == 304 and conditional:
                headers = {}
                conditional = False
                continue

            failure_kind = classify_http_failure(response.status_code)
            if failure_kind is not None:
                if response_origin:
                    record_service_failure(
                        ctx.service_outages,
                        response_origin,
                        f"Link origin {response_origin}",
                        failure_kind,
                        f"GET {redact_url_secrets(final_url)} returned HTTP "
                        f"{response.status_code}",
                        log,
                    )
                return None, True

            if response_origin:
                ctx.service_outages.record_available(response_origin)
            content_type = content_type_without_parameters(response)
            html = (
                None
                if content_type and content_type not in HTML_CONTENT_TYPES
                else response.text
            )
            return _response_cache_entry(response, html)


def _fresh_cached_resource(
    ctx: SyncContext,
    cached: LinkedResourceCacheEntry | None,
) -> tuple[bool, LinkedResourceCacheEntry | None]:
    if (
        cached is None
        or cached.fresh_until is None
        or time.time() >= cached.fresh_until
    ):
        return False, None
    if filters.should_skip_url(ctx, cached.final_url, "cached linked resource"):
        return True, None
    return True, cached


def _handle_link_request_error(
    ctx: SyncContext,
    url: str,
    request_origin: str | None,
    error: requests.RequestException,
    log: logging.Logger,
) -> tuple[None, bool]:
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
    return None, True


def _resolve_linked_resource(
    ctx: SyncContext,
    url: str,
    cached: LinkedResourceCacheEntry | None,
    log: logging.Logger,
) -> tuple[LinkedResourceCacheEntry | None, bool]:
    link_origin = normalized_http_origin(url)
    request_origin = link_origin
    use_cached, fresh_resource = _fresh_cached_resource(ctx, cached)
    if use_cached:
        return fresh_resource, True
    if link_origin and ctx.service_outages.should_skip(link_origin):
        return None, True

    def url_allowed(candidate: str) -> bool:
        return filters.require_url_allowed(ctx, candidate, "redirected link")

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
        resource, final_url, final_origin, cacheable = _head_linked_resource(
            ctx,
            url,
            cached,
            url_allowed,
        )
        request_origin = final_origin or request_origin
        if resource is not None:
            if request_origin:
                ctx.service_outages.record_available(request_origin)
            return resource, cacheable

        if not ctx.config.follow_links or (
            request_origin and ctx.service_outages.should_skip(request_origin)
        ):
            return None, True
        ctx.output.sync_progress.module_status("loading linked page")
        return _get_linked_resource(
            ctx,
            final_url,
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


def _scan_single_link(
    ctx: SyncContext,
    url: str,
    parent_node: Node,
    course_id: Any,
    module_title: Any,
    log: logging.Logger,
) -> bool:
    """Resolve one generic link and return whether it is a direct file."""
    if filters.should_skip_url(ctx, url, "link"):
        return False
    course_key = str(course_id)
    cache = ctx.linked_resources_by_course.setdefault(course_key, {})
    ctx.seen_linked_resources.add((course_key, url))
    if url in ctx.linked_resource_results:
        resource = ctx.linked_resource_results[url]
        if resource is not None:
            cache[url] = resource
    else:
        resource, cacheable = _resolve_linked_resource(ctx, url, cache.get(url), log)
        if cacheable:
            if resource is not None:
                cache[url] = resource
            ctx.linked_resource_results[url] = resource
        else:
            cache.pop(url, None)

    if resource is None:
        return False
    if resource.html is None:
        parent_node.add_child(
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
                link = urllib.parse.urljoin(str(base_url or ""), video_src)
                if not filters.should_skip_url(ctx, link, "embedded video"):
                    parent_node.add_child(
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
    module_title: Any,
) -> None:
    if not ctx.config.link_source_enabled("youtube"):
        return
    for match in YOUTUBE_LINK_RE.finditer(text):
        link = match.group(1)
        if filters.should_skip_url(ctx, link, "YouTube link"):
            continue
        video_id = youtube_video_id(link)
        if video_id is None:
            continue
        canonical_url = canonical_youtube_url(video_id)
        parent_node.add_child(
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
        if filters.should_skip_url(ctx, video_url, "Opencast link"):
            continue
        video_id = opencast_api.extract_episode_id(video_url)
        if not video_id:
            log.warning("Opencast: could not extract episode id from url %s", video_url)
            continue
        opencast_api.add_episode_nodes(
            ctx,
            parent_node,
            module_title,
            video_id,
            log,
            course_id=course_id,
        )


def _scan_emedia_links(
    ctx: SyncContext,
    text: str,
    parent_node: Node,
    module_title: Any,
    log: logging.Logger,
) -> None:
    if not ctx.config.link_source_enabled("emedia"):
        return
    for match in EMEDIA_LINK_RE.finditer(text):
        link = match.group(0)
        if filters.should_skip_url(ctx, link, "emedia link"):
            continue
        ctx.output.sync_progress.module_status("resolving emedia video")
        emedia_api.add_video_node(ctx, parent_node, link, module_title, log)


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
        text = text.replace("webservice/pluginfile.php", "pluginfile.php")
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

    _scan_youtube_links(ctx, text, parent_node, module_title)
    _scan_opencast_links(ctx, text, parent_node, course_id, module_title, log)
    _scan_emedia_links(ctx, text, parent_node, module_title, log)
    if ctx.config.link_source_enabled("sciebo"):
        sciebo_api.scan_public_shares(ctx, text, parent_node, log)
