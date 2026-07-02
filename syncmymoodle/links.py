import logging
import urllib.parse
from typing import cast

from bs4 import BeautifulSoup as bs

from syncmymoodle import sciebo as sciebo_api
from syncmymoodle.constants import OPENCAST_LINK_RE, YOUTUBE_LINK_RE
from syncmymoodle.context import SyncContext

logger = logging.getLogger(__name__)


def scan_html_text_for_links(
    html_text,
    base_url,
    parent_node,
    course_id,
    module_title,
    should_skip_url,
    scan_for_links,
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
                if not should_skip_url(link, "embedded video"):
                    parent_node.add_child(
                        video_src.split("/")[-1],
                        None,
                        "Embedded videojs",
                        url=link,
                    )

    scan_for_links(
        html_text,
        parent_node,
        course_id,
        module_title=module_title,
        single=False,
    )


def scan_for_links(
    ctx: SyncContext,
    text,
    parent_node,
    course_id,
    module_title,
    single,
    should_skip_url,
    content_type_without_parameters,
    scan_html_text_for_links,
    extract_opencast_episode_id,
    authenticate_opencast_episode,
    extract_track_from_episode,
    log: logging.Logger = logger,
) -> None:
    # A single link is supplied and the contents of it are checked
    if single:
        try:
            text = text.replace("webservice/pluginfile.php", "pluginfile.php")
            if should_skip_url(text, "link"):
                return
            response = ctx.session.head(text, allow_redirects=True)
            content_type = content_type_without_parameters(response)
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
                )
                # instantly return as it was a direct link
                return
            elif not ctx.config.get("nolinks"):
                response = ctx.session.get(text)
                scan_html_text_for_links(
                    response.text,
                    response.url or text,
                    parent_node,
                    course_id,
                    module_title=module_title,
                )
        except Exception:
            # Maybe the url is down?
            log.exception(f"Error while downloading url {text}")
    if ctx.config.get("nolinks"):
        return

    # Youtube videos
    if ctx.config.get("used_modules", {}).get("url", {}).get("youtube", {}):
        youtube_links = [
            match.group(1)
            # finds youtube.com, youtu.be and embed links
            for match in YOUTUBE_LINK_RE.finditer(text)
        ]
        for link in youtube_links:
            if should_skip_url(link, "YouTube link"):
                continue
            parent_node.add_child(
                f"Youtube: {module_title or link}", link, "Youtube", url=link
            )

    # OpenCast videos
    if ctx.config.get("used_modules", {}).get("url", {}).get("opencast", {}):
        opencast_links = OPENCAST_LINK_RE.findall(text)
        for vid in opencast_links:
            if should_skip_url(vid, "Opencast link"):
                continue
            vid_id = extract_opencast_episode_id(vid)
            if not vid_id:
                log.warning(f"Opencast: could not extract episode id from url {vid}")
                continue
            if not authenticate_opencast_episode(course_id, vid_id):
                continue
            vid = extract_track_from_episode(vid_id)
            if not vid:
                continue
            if should_skip_url(vid, "Opencast video URL"):
                continue

            parent_node.add_child(
                module_title or vid.split("/")[-1],
                vid_id,
                "Opencast",
                url=vid,
                additional_info=course_id,
            )

    # https://rwth-aachen.sciebo.de/s/XXX
    if ctx.config.get("used_modules", {}).get("url", {}).get("sciebo", {}):
        sciebo_api.scan_public_shares(ctx, text, parent_node, should_skip_url, log)
