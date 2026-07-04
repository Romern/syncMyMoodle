import logging
import urllib.parse
from typing import Any, cast

from bs4 import BeautifulSoup as bs

from syncmymoodle import downloader as downloader_api
from syncmymoodle import filters
from syncmymoodle import opencast as opencast_api
from syncmymoodle import sciebo as sciebo_api
from syncmymoodle.constants import OPENCAST_LINK_RE, YOUTUBE_LINK_RE
from syncmymoodle.context import SyncContext
from syncmymoodle.node import Node

logger = logging.getLogger(__name__)


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
        soup = bs(html_text, features="lxml")
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
            content_type = downloader_api.content_type_without_parameters(response)
            if "youtube.com" in text or "youtu.be" in text:
                # workaround for youtube providing bad headers when using HEAD
                pass
            elif (
                200 <= response.status_code < 300
                and content_type
                and content_type not in {"text/html", "application/xhtml+xml"}
            ):
                # non html links, assume the filename is in the path
                filename = urllib.parse.urlsplit(text).path.split("/")[-1]
                parent_node.add_child(
                    filename,
                    None,
                    f'Linked file [{response.headers["Content-Type"]}]',
                    url=text,
                    etag=response.headers.get("ETag"),
                )
                # instantly return as it was a direct link
                return
            elif not ctx.config.nolinks:
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
    if ctx.config.nolinks:
        return

    # Youtube videos
    if ctx.config.url_module_enabled("youtube"):
        youtube_links = [
            match.group(1)
            # finds youtube.com, youtu.be and embed links
            for match in YOUTUBE_LINK_RE.finditer(text)
        ]
        for link in youtube_links:
            if filters.should_skip_url(ctx.config, link, "YouTube link", log):
                continue
            parent_node.add_child(
                f"Youtube: {module_title or link}", link, "Youtube", url=link
            )

    # OpenCast videos
    if ctx.config.url_module_enabled("opencast"):
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
                additional_info=course_id,
                etag=track.remote_marker,
            )

    # https://rwth-aachen.sciebo.de/s/XXX
    if ctx.config.url_module_enabled("sciebo"):
        sciebo_api.scan_public_shares(ctx, text, parent_node, log)
