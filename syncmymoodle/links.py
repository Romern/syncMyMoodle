import logging
import urllib.parse
from typing import Any, cast

from yt_dlp.extractor.youtube import YoutubeIE  # type: ignore[import-untyped]

from syncmymoodle import filters
from syncmymoodle import opencast as opencast_api
from syncmymoodle import sciebo as sciebo_api
from syncmymoodle.constants import OPENCAST_LINK_RE, YOUTUBE_LINK_RE, YOUTUBE_WATCH_URL
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import (
    HTML_CONTENT_TYPES,
    content_length,
    content_type_without_parameters,
    filename_from_url,
    parse_html,
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
                if not filters.should_skip_url(ctx.config, link, "embedded video", log):
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


def scan_for_links(
    ctx: SyncContext,
    text: str,
    parent_node: Node,
    course_id: Any,
    module_title: Any = None,
    single: bool = False,
    log: logging.Logger = logger,
) -> None:
    # A single link is supplied and the contents of it are checked
    if single:
        try:
            text = text.replace("webservice/pluginfile.php", "pluginfile.php")
            if filters.should_skip_url(ctx.config, text, "link", log):
                return
            response = ctx.require_session().head(text, allow_redirects=True)
            content_type = content_type_without_parameters(response)
            if "youtube.com" in text or "youtu.be" in text:
                # workaround for youtube providing bad headers when using HEAD
                pass
            elif (
                200 <= response.status_code < 300
                and content_type
                and content_type not in HTML_CONTENT_TYPES
            ):
                # non html links, assume the filename is in the path
                filename = filename_from_url(text)
                parent_node.add_child(
                    filename,
                    None,
                    f"Linked file [{response.headers['Content-Type']}]",
                    url=text,
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
            elif ctx.config.follow_links:
                response = ctx.require_session().get(text)
                scan_html_text_for_links(
                    ctx,
                    response.text,
                    response.url or text,
                    parent_node,
                    course_id,
                    module_title=module_title,
                )
        except Exception:
            # Maybe the url is down?
            log.exception(f"Error while downloading url {text}")
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
            if filters.should_skip_url(ctx.config, link, "YouTube link", log):
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
            if filters.should_skip_url(ctx.config, vid, "Opencast link", log):
                continue
            vid_id = opencast_api.extract_episode_id(vid)
            if not vid_id:
                log.warning(f"Opencast: could not extract episode id from url {vid}")
                continue
            if not opencast_api.authenticate_episode(ctx, course_id, vid_id, log):
                continue
            track = opencast_api.resolve_track_from_episode(ctx, vid_id, log)
            if track is None:
                continue
            if filters.should_skip_url(
                ctx.config, track.url, "Opencast video URL", log
            ):
                continue

            parent_node.add_child(
                module_title or track.url.split("/")[-1],
                vid_id,
                "Opencast",
                url=track.url,
                etag=track.remote_marker,
                etag_kind=track.remote_marker_kind,
                remote_size=track.size,
            )

    # https://rwth-aachen.sciebo.de/s/XXX
    if ctx.config.link_source_enabled("sciebo"):
        sciebo_api.scan_public_shares(ctx, text, parent_node, log)
