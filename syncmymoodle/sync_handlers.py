import html
import logging
import tempfile
import urllib.parse
import zipfile
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Callable, cast

import requests

from syncmymoodle import filters, moodle_files
from syncmymoodle import links as links_api
from syncmymoodle import moodle as moodle_api
from syncmymoodle import opencast as opencast_api
from syncmymoodle.constants import HTTP_TIMEOUT_SECONDS, MOODLE_URL
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import (
    content_length,
    copy_capped_body,
    parse_html,
    safe_request_error,
)
from syncmymoodle.node import Node

logger = logging.getLogger(__name__)
H5P_PACKAGE_MAX_BYTES = 2 * 1024**3
H5P_PACKAGE_MEMORY_BYTES = 16 * 1024**2
H5P_CONTENT_MAX_BYTES = 10 * 1024 * 1024


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
OPENCAST_SERIES_PAGE_SIZE = 100


def register_handler(handler: Handler) -> Handler:
    """Register a sync module handler so ``handle_module`` dispatches to it."""
    MODULE_HANDLERS.append(handler)
    return handler


def _opencast_series_episodes(
    ctx: SyncContext,
    series_id: str,
    log: logging.Logger,
) -> list[tuple[str, str]] | None:
    episodes: list[tuple[str, str]] = []
    seen_episode_ids: set[str] = set()
    offset = 0
    while True:
        query = urllib.parse.urlencode(
            {
                "limit": OPENCAST_SERIES_PAGE_SIZE,
                "offset": offset,
                "sid": series_id,
            }
        )
        page = opencast_api.fetch_result_list(
            ctx,
            f"{opencast_api.OPENCAST_SEARCH_URL}?{query}",
            f"series {series_id}",
            log,
        )
        if page is None:
            return None
        new_episodes: list[tuple[str, str]] = []
        for episode in page:
            mediapackage = (
                episode.get("mediapackage") if isinstance(episode, dict) else None
            )
            if not isinstance(mediapackage, dict):
                log.warning(
                    "Opencast: series %s contains episode without id",
                    series_id,
                )
                continue
            episode_id = mediapackage.get("id")
            if not isinstance(episode_id, str) or not episode_id:
                log.warning(
                    "Opencast: series %s contains episode without id",
                    series_id,
                )
                continue
            if episode_id in seen_episode_ids:
                continue
            seen_episode_ids.add(episode_id)
            raw_title = mediapackage.get("title")
            title = (
                raw_title if isinstance(raw_title, str) and raw_title else episode_id
            )
            new_episodes.append((episode_id, title))
        if page and not new_episodes:
            log.warning(
                "Opencast: series %s made no pagination progress at offset %s; "
                "stopping",
                series_id,
                offset,
            )
            return episodes
        episodes.extend(new_episodes)
        if len(page) < OPENCAST_SERIES_PAGE_SIZE:
            return episodes
        offset += OPENCAST_SERIES_PAGE_SIZE


def _read_h5p_content(
    session: Any,
    package_url: str,
    module_id: Any,
    log: logging.Logger,
) -> str | None:
    try:
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

        account = ctx.require_moodle_account()
        ass = ass["introattachments"] + moodle_api.get_assignment_submission_files(
            ctx.require_session(),
            account.wstoken,
            account.user_id,
            assignment_id,
        )
        for c in ass:
            if filters.should_skip_url(ctx, c.get("fileurl"), "assignment file"):
                continue
            moodle_files.add_moodle_file_node(
                assignment_node,
                c.get("filepath", "/"),
                c["filename"],
                c["fileurl"],
                "Assignment File",
                c["fileurl"],
                timemodified=c.get("timemodified"),
                remote_size=c.get("filesize"),
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
def handle_embedded_link_module(  # noqa: C901 - legacy handler awaiting decomposition
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    ctx = module_context.ctx
    section_node = module_context.section_node
    course_id = module_context.course_id
    log = module_context.log

    # Get embedded videos in pages or labels
    if (
        module["modname"]
        not in [
            "page",
            "label",
            "h5pactivity",
        ]
        or not ctx.config.follow_links
    ):
        return

    if module["modname"] == "page":
        opencast_enabled = ctx.config.link_source_enabled("opencast")
        index_content = next(
            (
                content
                for content in module.get("contents") or []
                if content.get("filename") == "index.html" and content.get("fileurl")
            ),
            None,
        )
        html_url = (
            index_content["fileurl"]
            if index_content is not None
            else module.get("url") or f"{MOODLE_URL}mod/page/view.php?id={module['id']}"
        )
        scan_page_links = not filters.should_skip_url(ctx, html_url, "page link")
        if opencast_enabled or scan_page_links:
            try:
                response = ctx.require_session().get(
                    html_url,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
            except requests.RequestException as error:
                log.warning(
                    "Failed to fetch page module %s: %s",
                    module["id"],
                    safe_request_error(error),
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
                    html = parse_html(response.text)
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
                            ctx, track.url, "Opencast video URL"
                        ):
                            continue

                        opencast_api.add_track_node(
                            section_node,
                            module["name"],
                            vid_id,
                            track,
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
        activity = module_instance(
            ctx,
            module,
            course_id,
            ctx.h5p_activity_cache,
            moodle_api.get_h5pactivities_by_course,
        )
        package_files = activity.get("package") if activity else None
        if isinstance(package_files, dict):
            package_files = [package_files]
        package_url = next(
            (
                item.get("fileurl")
                for item in package_files or []
                if isinstance(item, dict) and item.get("fileurl")
            ),
            None,
        )
        if isinstance(package_url, str):
            content = _read_h5p_content(
                ctx.require_session(), package_url, module["id"], log
            )
            if content is not None:
                links_api.scan_for_links(
                    ctx,
                    content,
                    section_node,
                    course_id,
                    module_title=module["name"],
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
def handle_opencast_lti_module(  # noqa: C901 - legacy handler awaiting decomposition
    module_context: ModuleContext,
    module: dict[str, Any],
) -> None:
    ctx = module_context.ctx
    section_node = module_context.section_node
    course_node = module_context.course_node
    log = module_context.log

    # New OpenCast integration
    if module["modname"] != "lti" or not ctx.config.link_source_enabled("opencast"):
        return

    instance = module_instance(
        ctx,
        module,
        module_context.course_id,
        ctx.lti_instance_cache,
        moodle_api.get_ltis_by_course,
    )
    tool_id = instance.get("id") if instance else module.get("instance")
    if not isinstance(tool_id, int):
        log.warning("Opencast: LTI module %s has no tool instance id", module["id"])
        return
    launch_data = moodle_api.get_lti_launch_data(
        ctx.require_session(), ctx.require_moodle_account().wstoken, tool_id
    )
    if launch_data is None:
        return
    endpoint = launch_data.get("endpoint")
    if not isinstance(endpoint, str):
        log.warning("Opencast: LTI module %s has no launch endpoint", module["id"])
        return
    engage_data = {
        str(item["name"]): item.get("value", "")
        for item in launch_data.get("parameters") or []
        if isinstance(item, dict) and item.get("name")
    }
    engage_series_id = engage_data.get("custom_series")
    engage_single_id = engage_data.get("custom_id")
    name = engage_data.get("resource_link_title") or module["name"]

    if engage_series_id:
        # Found an Opencast "series" page
        series_id = engage_series_id

        if not opencast_api.submit_lti_form(
            ctx,
            engage_data,
            f"LTI series module {module['id']}",
            log,
            endpoint=endpoint,
        ):
            return

        episodes = _opencast_series_episodes(ctx, series_id, log)
        if episodes is None:
            return
        series_node = cast(Node, course_node.add_child(name, series_id, "Section"))

        for episode_id, episode_title in episodes:
            track = opencast_api.resolve_track_from_episode(ctx, episode_id, log)
            if track is None:
                continue
            if filters.should_skip_url(ctx, track.url, "Opencast video URL"):
                continue
            opencast_api.add_track_node(
                series_node,
                episode_title,
                episode_id,
                track,
            )
    else:
        if not engage_single_id:
            log.info(
                "Opencast LTI module %s has neither custom_id nor custom_series",
                module["id"],
            )
        else:
            if not opencast_api.submit_lti_form(
                ctx,
                engage_data,
                f"LTI module {module['id']}",
                log,
                endpoint=endpoint,
            ):
                return
            track = opencast_api.resolve_track_from_episode(ctx, engage_single_id, log)
            if track is None:
                return
            if filters.should_skip_url(ctx, track.url, "Opencast video URL"):
                return
            opencast_api.add_track_node(
                section_node,
                name,
                engage_single_id,
                track,
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

    instance = module_instance(
        ctx,
        module,
        module_context.course_id,
        ctx.quiz_instance_cache,
        moodle_api.get_quizzes_by_course,
    )
    quiz_id = instance.get("id") if instance else module.get("instance")
    if not isinstance(quiz_id, int):
        return
    attempts = moodle_api.get_quiz_attempts(
        ctx.require_session(), ctx.require_moodle_account().wstoken, quiz_id
    )
    for index, attempt in enumerate(attempts, 1):
        attempt_id = attempt.get("id")
        if not isinstance(attempt_id, int):
            continue
        review = moodle_api.get_quiz_attempt_review(
            ctx.require_session(), ctx.require_moodle_account().wstoken, attempt_id
        )
        if review is None:
            continue
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
        body = "".join(parts)
        name = f"{module['name']}, Versuch {index}"
        review_url = f"{MOODLE_URL}mod/quiz/review.php?attempt={attempt_id}"
        ctx.quiz_review_cache[review_url] = (
            "<!doctype html><html><head><title>"
            f"{html.escape(name)}</title></head><body>{body}</body></html>"
        )
        section_node.add_child(
            name,
            attempt_id,
            "Quiz",
            url=review_url,
        )


def handle_module(module_context: ModuleContext, module: dict[str, Any]) -> None:
    for handler in MODULE_HANDLERS:
        handler(module_context, module)
