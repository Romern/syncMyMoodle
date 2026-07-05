import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, cast

from bs4 import BeautifulSoup as bs

from syncmymoodle import filters, moodle_files, pathing
from syncmymoodle import links as links_api
from syncmymoodle import moodle as moodle_api
from syncmymoodle import opencast as opencast_api
from syncmymoodle.constants import MOODLE_URL
from syncmymoodle.context import SyncContext
from syncmymoodle.node import Node

logger = logging.getLogger(__name__)


@dataclass
class ModuleContext:
    ctx: SyncContext
    course_id: Any
    course_node: Node
    section_node: Node
    assignments_by_cmid: Any
    folders_by_coursemodule: Any
    log: logging.Logger = logger


Handler = Callable[[ModuleContext, dict[str, Any]], None]

# Handlers register themselves here via @register_handler and run, per module,
# in registration (definition) order.
MODULE_HANDLERS: list[Handler] = []


def register_handler(handler: Handler) -> Handler:
    """Register a sync module handler so ``handle_module`` dispatches to it."""
    MODULE_HANDLERS.append(handler)
    return handler


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
    if module["modname"] == "assign" and ctx.config.module_enabled("assign"):
        ass = assignments_by_cmid.get(module["id"])
        if not ass:
            return
        assignment_id = ass["id"]
        assignment_name = module["name"]
        assignment_node = section_node.add_child(
            assignment_name, assignment_id, "Assignment"
        )
        if assignment_node is None:
            return

        assignment_intro = ass.get("intro")
        if assignment_intro:
            links_api.scan_for_links(
                ctx,
                assignment_intro,
                assignment_node,
                course_id,
                module_title=assignment_name,
            )

        ass = ass["introattachments"] + moodle_api.get_assignment_submission_files(
            ctx.require_session(),
            cast(str, ctx.wstoken),
            ctx.user_id,
            assignment_id,
        )
        for c in ass:
            if filters.should_skip_url(ctx.config, c.get("fileurl"), "assignment file"):
                continue
            moodle_files.add_moodle_file_node(
                assignment_node,
                c.get("filepath", "/"),
                c["filename"],
                c["fileurl"],
                "Assignment File",
                c["fileurl"],
                timemodified=c.get("timemodified"),
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
    if module["modname"] == "resource" and not ctx.config.module_enabled("resource"):
        return
    for c in module.get("contents", []):
        file_url = c.get("fileurl")
        if not file_url:
            continue
        if filters.should_skip_url(ctx.config, file_url, "resource link"):
            continue
        if moodle_files.is_direct_moodle_file_content(module, c):
            moodle_files.add_moodle_content_file_node(section_node, c)
        elif not (module["modname"] == "page" and c.get("filename") == "index.html"):
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
    if module["modname"] == "folder" and ctx.config.module_enabled("folder"):
        folder_node = section_node.add_child(module["name"], module["id"], "Folder")
        if folder_node is None:
            return

        # Scan intro for links
        folder_info = folders_by_coursemodule.get(module["id"])
        if folder_info and folder_info.get("intro"):
            links_api.scan_for_links(ctx, folder_info["intro"], folder_node, course_id)

        for c in module.get("contents", []):
            if filters.should_skip_url(ctx.config, c.get("fileurl"), "folder file"):
                continue
            moodle_files.add_moodle_file_node(
                folder_node,
                c.get("filepath", "/"),
                c["filename"],
                c["fileurl"],
                "Folder File",
                c["fileurl"],
                timemodified=c.get("timemodified"),
            )


@register_handler
def handle_embedded_link_module(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    ctx = module_context.ctx
    section_node = module_context.section_node
    course_id = module_context.course_id
    log = module_context.log

    # Get embedded videos in pages or labels
    if module["modname"] not in [
        "page",
        "label",
        "h5pactivity",
    ] or not ctx.config.module_enabled("url"):
        return

    if module["modname"] == "page":
        opencast_enabled = ctx.config.url_module_enabled("opencast")
        html_url = (
            module.get("url") or f"{MOODLE_URL}mod/page/view.php?id={module['id']}"
        )
        scan_page_links = not ctx.config.nolinks and not filters.should_skip_url(
            ctx.config, html_url, "page link"
        )
        if opencast_enabled or scan_page_links:
            try:
                response = ctx.require_session().get(html_url)
            except Exception:
                log.exception(
                    "Failed to fetch page module %s",
                    module["id"],
                )
                response = None
            if response and not (200 <= response.status_code < 300):
                log.warning(
                    "Page module %s returned status %s",
                    module["id"],
                    response.status_code,
                )
                response = None
            if response:
                if opencast_enabled:
                    html = bs(
                        response.text,
                        features="lxml",
                    )
                    for iframe in html.find_all("iframe"):
                        iframe_src_value = iframe.get("src")
                        if not iframe_src_value:
                            continue
                        iframe_src = urllib.parse.urljoin(
                            response.url or html_url,
                            cast(str, iframe_src_value),
                        )
                        vid_id = opencast_api.extract_episode_id(iframe_src)
                        if not vid_id:
                            continue
                        if not opencast_api.authenticate_episode(
                            ctx, course_id, vid_id, log
                        ):
                            continue
                        track = opencast_api.resolve_track_from_episode(
                            ctx, vid_id, log
                        )
                        if track is None:
                            continue

                        if filters.should_skip_url(
                            ctx.config, track.url, "Opencast video URL"
                        ):
                            continue

                        section_node.add_child(
                            module["name"],
                            vid_id,
                            "Opencast",
                            url=track.url,
                            etag=track.remote_marker,
                            etag_kind=track.remote_marker_kind,
                        )

                if scan_page_links:
                    links_api.scan_html_text_for_links(
                        ctx,
                        response.text,
                        response.url or html_url,
                        section_node,
                        course_id,
                        module_title=module["name"],
                    )
    # "Interactive" h5p videos
    elif module["modname"] == "h5pactivity":
        html_url = f"{MOODLE_URL}mod/h5pactivity/view.php?id={module['id']}"
        html = bs(
            ctx.require_session().get(html_url).text,
            features="lxml",
        )
        # Get h5p iframe
        h5p_iframe = html.find("iframe")
        iframe_src_value = h5p_iframe.get("src") if h5p_iframe else None
        if iframe_src_value:
            iframe_src = urllib.parse.urljoin(html_url, cast(str, iframe_src_value))
            iframe_html = str(
                bs(
                    ctx.require_session().get(iframe_src).text,
                    features="lxml",
                )
            )
            # Moodle devs dont know how to use CDATA correctly, so we need to remove all backslashes
            sanitized_html = iframe_html.replace("\\", "")
        else:
            # H5P outside iframes
            sanitized_html = str(html).replace("\\", "")

        links_api.scan_for_links(
            ctx,
            sanitized_html,
            section_node,
            course_id,
            module_title=module["modname"],
            single=False,
        )
    else:
        links_api.scan_for_links(
            ctx,
            module.get("description", ""),
            section_node,
            course_id,
            module_title=module["name"],
        )


@register_handler
def handle_opencast_lti_module(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    ctx = module_context.ctx
    section_node = module_context.section_node
    course_node = module_context.course_node
    log = module_context.log

    # New OpenCast integration
    if module["modname"] != "lti" or not ctx.config.url_module_enabled("opencast"):
        return

    info_url = f"{MOODLE_URL}mod/lti/launch.php?id={module['id']}&triggerview=0"
    try:
        info_response = ctx.require_session().get(info_url)
    except Exception:
        log.exception(
            "Opencast: failed to fetch LTI module %s",
            module["id"],
        )
        return
    if not (200 <= info_response.status_code < 300):
        log.warning(
            "Opencast: LTI module %s returned status %s",
            module["id"],
            info_response.status_code,
        )
        opencast_api.log_backend_issue(ctx, info_response.text, log)
        return

    info_res = bs(info_response.text, features="lxml")

    engage_series_id = opencast_api.get_input_value(info_res, "custom_series")
    engage_single_id = opencast_api.get_input_value(info_res, "custom_id")
    name = (
        opencast_api.get_input_value(info_res, "resource_link_title") or module["name"]
    )
    engage_data = opencast_api.extract_lti_form_data(info_res)

    if engage_series_id:
        # Found an Opencast "series" page
        series_id = engage_series_id

        series_node = cast(Node, course_node.add_child(name, series_id, "Section"))

        if not opencast_api.submit_lti_form(
            ctx, engage_data, f"LTI series module {module['id']}", log
        ):
            return

        series_url = (
            f"{opencast_api.OPENCAST_SEARCH_URL}?limit=100&offset=0&sid={series_id}"
        )
        series_response = opencast_api.fetch_json(
            ctx, series_url, f"series {series_id}", log
        )
        if series_response is None:
            return

        for episode in opencast_api.get_result_list(
            ctx, series_response, f"series {series_id}", log
        ):
            if not isinstance(episode, dict):
                continue
            mediapackage = episode.get("mediapackage", {})
            if not isinstance(mediapackage, dict):
                continue
            episode_id = mediapackage.get("id")
            if not episode_id:
                log.warning(
                    "Opencast: series %s contains episode without id",
                    series_id,
                )
                continue
            track = opencast_api.resolve_track_from_episode(ctx, episode_id, log)
            if track is None:
                continue
            if filters.should_skip_url(ctx.config, track.url, "Opencast video URL"):
                continue
            series_node.add_child(
                mediapackage.get("title") or episode_id,
                episode_id,
                "Opencast",
                url=track.url,
                etag=track.remote_marker,
                etag_kind=track.remote_marker_kind,
            )
    else:
        if not engage_single_id:
            log.info("Failed to find either custom_id or custom_series on lti page.")
            log.info("------LTI-ERROR-HTML------")
            log.info(f"url: {info_url}")
            log.info(info_res)
        else:
            if not opencast_api.submit_lti_form(
                ctx, engage_data, f"LTI module {module['id']}", log
            ):
                return
            track = opencast_api.resolve_track_from_episode(ctx, engage_single_id, log)
            if track is None:
                return
            if filters.should_skip_url(ctx.config, track.url, "Opencast video URL"):
                return
            section_node.add_child(
                name,
                engage_single_id,
                "Opencast",
                url=track.url,
                etag=track.remote_marker,
                etag_kind=track.remote_marker_kind,
            )


@register_handler
def handle_quiz_module(
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    ctx = module_context.ctx
    section_node = module_context.section_node

    # Integration for Quizzes
    if module["modname"] != "quiz" or not ctx.config.url_module_enabled("quiz"):
        return

    info_url = f"{MOODLE_URL}mod/quiz/view.php?id={module['id']}"
    info_res = bs(ctx.require_session().get(info_url).text, features="lxml")
    attempts = info_res.find_all(
        "a",
        {"title": "Überprüfung der eigenen Antworten dieses Versuchs"},
    )
    attempt_cnt = 0
    for attempt in attempts:
        attempt_cnt += 1
        review_url = cast(str, attempt.get("href"))
        quiz_res = bs(
            ctx.require_session().get(review_url).text,
            features="lxml",
        )
        title = cast(Any, quiz_res.find("title"))
        name = (
            title.get_text().replace(": Überprüfung des Testversuchs", "")
            + ", Versuch "
            + str(attempt_cnt)
        )
        section_node.add_child(
            pathing.sanitize_path_part(name),
            urllib.parse.urlparse(review_url)[1],
            "Quiz",
            url=review_url,
        )


def handle_module(module_context: ModuleContext, module: dict[str, Any]) -> None:
    for handler in MODULE_HANDLERS:
        handler(module_context, module)
