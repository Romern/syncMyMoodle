import logging
import urllib.parse
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
from syncmymoodle.context import SyncContext
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
from syncmymoodle.node import Node, RemoteMarkerKind

logger = logging.getLogger(__name__)


def youtube_video_id(link: str) -> str | None:
    try:
        return cast(str, YoutubeIE.extract_id(link))
    except Exception:
        return None


def canonical_youtube_url(video_id: str) -> str:
    return YOUTUBE_WATCH_URL.format(video_id=video_id)


def youtube_video_id_from_node(node: Node) -> str | None:
    if node.type != "Youtube":
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
    )


def scan_for_links(  # noqa: C901 - legacy parser awaiting decomposition
    ctx: SyncContext,
    text: str,
    parent_node: Node,
    course_id: Any,
    module_title: Any = None,
    single: bool = False,
    log: logging.Logger = logger,
) -> None:
    # A single link is supplied and the contents of it are checked
    link_origin = normalized_http_origin(text) if single else None
    is_sciebo_share = (
        ctx.config.link_source_enabled("sciebo")
        and SCIEBO_LINK_RE.fullmatch(text.split("?", 1)[0].split("#", 1)[0].rstrip("/"))
        is not None
    )
    is_emedia_video = (
        ctx.config.link_source_enabled("emedia")
        and emedia_api.extract_video_id(text) is not None
    )
    origin_is_unavailable = bool(
        link_origin and ctx.service_outages.should_skip(link_origin)
    )
    request_origin = link_origin
    if (
        single
        and not is_sciebo_share
        and not is_emedia_video
        and not origin_is_unavailable
    ):
        try:
            text = text.replace("webservice/pluginfile.php", "pluginfile.php")
            if filters.should_skip_url(ctx, text, "link"):
                return

            ctx.output.sync_progress.module_status("checking linked resource")

            def url_allowed(url: str) -> bool:
                return filters.require_url_allowed(
                    ctx,
                    url,
                    "redirected link",
                )

            response = request_following_safe_redirects(
                ctx.require_session(),
                "HEAD",
                text,
                url_allowed,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            with closing(response):
                final_url = response.url or text
                request_origin = normalized_http_origin(final_url) or link_origin
                content_type = content_type_without_parameters(response)
                if "youtube.com" in text or "youtu.be" in text:
                    # workaround for youtube providing bad headers when using HEAD
                    pass
                elif (
                    200 <= response.status_code < 300
                    and content_type
                    and content_type not in HTML_CONTENT_TYPES
                ):
                    if request_origin:
                        ctx.service_outages.record_available(request_origin)
                    # non html links, assume the filename is in the path
                    filename = filename_from_url(final_url)
                    parent_node.add_child(
                        filename,
                        None,
                        f"Linked file [{content_type}]",
                        url=final_url,
                        etag=response.headers.get("ETag"),
                        etag_kind=(
                            RemoteMarkerKind.OPAQUE
                            if response.headers.get("ETag")
                            else None
                        ),
                        remote_size=content_length(response),
                    )
                    # instantly return as it was a direct link
                    return
            target_is_unavailable = bool(
                request_origin and ctx.service_outages.should_skip(request_origin)
            )
            if ctx.config.follow_links and not target_is_unavailable:
                ctx.output.sync_progress.module_status("loading linked page")
                response = request_following_safe_redirects(
                    ctx.require_session(),
                    "GET",
                    final_url,
                    url_allowed,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                with closing(response):
                    response_origin = (
                        normalized_http_origin(response.url or final_url)
                        or request_origin
                    )
                    if request_origin and response_origin != request_origin:
                        ctx.service_outages.record_available(request_origin)
                    request_origin = response_origin
                    failure_kind = classify_http_failure(response.status_code)
                    if failure_kind is None:
                        if request_origin:
                            ctx.service_outages.record_available(request_origin)
                        scan_html_text_for_links(
                            ctx,
                            response.text,
                            response.url or final_url,
                            parent_node,
                            course_id,
                            module_title=module_title,
                        )
                    else:
                        if request_origin:
                            assert failure_kind is not None
                            record_service_failure(
                                ctx.service_outages,
                                request_origin,
                                f"Link origin {request_origin}",
                                failure_kind,
                                f"GET {redact_url_secrets(response.url or final_url)} "
                                f"returned HTTP {response.status_code}",
                                log,
                            )
        except requests.RequestException as error:
            reason = (
                f"request for {redact_url_secrets(text)} failed: "
                f"{safe_request_error(error)}"
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
    if not ctx.config.follow_links:
        return

    # Youtube videos
    if ctx.config.link_source_enabled("youtube"):
        youtube_links = [
            match.group(1)
            # finds youtube.com, youtu.be and embed links
            for match in YOUTUBE_LINK_RE.finditer(text)
        ]
        for link in youtube_links:
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
            )

    # OpenCast videos
    if ctx.config.link_source_enabled("opencast"):
        opencast_links = OPENCAST_LINK_RE.findall(text)
        for vid in opencast_links:
            if filters.should_skip_url(ctx, vid, "Opencast link"):
                continue
            vid_id = opencast_api.extract_episode_id(vid)
            if not vid_id:
                log.warning(f"Opencast: could not extract episode id from url {vid}")
                continue
            ctx.output.sync_progress.module_status("authenticating Opencast video")
            if not opencast_api.authenticate_episode(ctx, course_id, vid_id, log):
                continue
            ctx.output.sync_progress.module_status("resolving Opencast video")
            opencast_api.add_episode_nodes(
                ctx,
                parent_node,
                module_title,
                vid_id,
                log,
            )

    # VEIRA videos on the separate emedia Medizin Moodle service
    if ctx.config.link_source_enabled("emedia"):
        for match in EMEDIA_LINK_RE.finditer(text):
            link = match.group(0)
            if filters.should_skip_url(ctx, link, "emedia link"):
                continue
            ctx.output.sync_progress.module_status("resolving emedia video")
            emedia_api.add_video_node(ctx, parent_node, link, module_title, log)

    # https://rwth-aachen.sciebo.de/s/XXX
    if ctx.config.link_source_enabled("sciebo"):
        sciebo_api.scan_public_shares(ctx, text, parent_node, log)
