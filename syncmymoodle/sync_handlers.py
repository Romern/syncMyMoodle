import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any, cast

from bs4 import BeautifulSoup as bs

logger = logging.getLogger(__name__)


@dataclass
class ModuleServices:
    add_moodle_file_node: Any
    add_moodle_content_file_node: Any
    get_assignment_submission_files: Any
    authenticate_opencast_episode: Any
    extract_lti_form_data: Any
    extract_opencast_episode_id: Any
    extract_track_from_episode: Any
    fetch_opencast_json: Any
    get_input_value: Any
    get_opencast_result_list: Any
    is_direct_moodle_file_content: Any
    log_opencast_backend_issue: Any
    scan_html_text_for_links: Any
    scan_for_links: Any
    should_skip_url: Any
    submit_opencast_lti_form: Any


def handle_assignment_module(
    ctx,
    module,
    section_node,
    course_id,
    assignments_by_cmid,
    services: ModuleServices,
) -> None:
    # Get Assignments
    if module["modname"] == "assign" and ctx.config.get("used_modules", {}).get(
        "assign", {}
    ):
        ass = assignments_by_cmid.get(module["id"])
        if not ass:
            return
        assignment_id = ass["id"]
        assignment_name = module["name"]
        assignment_node = section_node.add_child(
            assignment_name, assignment_id, "Assignment"
        )

        assignment_intro = ass.get("intro")
        if assignment_intro:
            services.scan_for_links(
                assignment_intro,
                assignment_node,
                course_id,
                module_title=assignment_name,
            )

        ass = ass["introattachments"] + services.get_assignment_submission_files(
            assignment_id
        )
        for c in ass:
            if services.should_skip_url(c.get("fileurl"), "assignment file"):
                continue
            services.add_moodle_file_node(
                assignment_node,
                c.get("filepath", "/"),
                c["filename"],
                c["fileurl"],
                "Assignment File",
                c["fileurl"],
                timemodified=c.get("timemodified"),
            )


def handle_resource_like_module(
    ctx,
    module,
    section_node,
    course_id,
    services: ModuleServices,
) -> None:
    # Get Resources or URLs
    if module["modname"] not in [
        "resource",
        "url",
        "book",
        "page",
        "pdfannotator",
    ]:
        return
    if module["modname"] == "resource" and not ctx.config.get("used_modules", {}).get(
        "resource", {}
    ):
        return
    for c in module.get("contents", []):
        file_url = c.get("fileurl")
        if not file_url:
            continue
        if services.should_skip_url(file_url, "resource link"):
            continue
        if services.is_direct_moodle_file_content(module, c):
            services.add_moodle_content_file_node(section_node, c)
        elif not (module["modname"] == "page" and c.get("filename") == "index.html"):
            services.scan_for_links(
                file_url,
                section_node,
                course_id,
                single=True,
                module_title=module["name"],
            )


def handle_folder_module(
    ctx,
    module,
    section_node,
    course_id,
    folders_by_coursemodule,
    services: ModuleServices,
) -> None:
    # Get Folders
    if module["modname"] == "folder" and ctx.config.get("used_modules", {}).get(
        "folder", {}
    ):
        folder_node = section_node.add_child(module["name"], module["id"], "Folder")

        # Scan intro for links
        folder_info = folders_by_coursemodule.get(module["id"])
        if folder_info and folder_info.get("intro"):
            services.scan_for_links(folder_info["intro"], folder_node, course_id)

        for c in module.get("contents", []):
            if services.should_skip_url(c.get("fileurl"), "folder file"):
                continue
            services.add_moodle_file_node(
                folder_node,
                c.get("filepath", "/"),
                c["filename"],
                c["fileurl"],
                "Folder File",
                c["fileurl"],
                timemodified=c.get("timemodified"),
            )


def handle_embedded_link_module(
    ctx,
    module,
    section_node,
    course_id,
    services: ModuleServices,
    log: logging.Logger = logger,
) -> None:
    # Get embedded videos in pages or labels
    if module["modname"] not in [
        "page",
        "label",
        "h5pactivity",
    ] or not ctx.config.get(
        "used_modules", {}
    ).get("url", {}):
        return

    if module["modname"] == "page":
        opencast_enabled = (
            ctx.config.get("used_modules", {}).get("url", {}).get("opencast", {})
        )
        html_url = (
            module.get("url")
            or f'https://moodle.rwth-aachen.de/mod/page/view.php?id={module["id"]}'
        )
        scan_page_links = not ctx.config.get(
            "nolinks"
        ) and not services.should_skip_url(html_url, "page link")
        if opencast_enabled or scan_page_links:
            try:
                response = ctx.session.get(html_url)
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
                        vid_id = services.extract_opencast_episode_id(iframe_src)
                        if not vid_id:
                            continue
                        if not services.authenticate_opencast_episode(
                            course_id, vid_id
                        ):
                            continue
                        vid = services.extract_track_from_episode(vid_id)
                        if not vid:
                            continue

                        if services.should_skip_url(vid, "Opencast video URL"):
                            continue

                        section_node.add_child(
                            module["name"],
                            vid_id,
                            "Opencast",
                            url=vid,
                            additional_info=course_id,
                        )

                if scan_page_links:
                    services.scan_html_text_for_links(
                        response.text,
                        response.url or html_url,
                        section_node,
                        course_id,
                        module_title=module["name"],
                    )
    # "Interactive" h5p videos
    elif module["modname"] == "h5pactivity":
        html_url = (
            f'https://moodle.rwth-aachen.de/mod/h5pactivity/view.php?id={module["id"]}'
        )
        html = bs(
            ctx.session.get(html_url).text,
            features="lxml",
        )
        # Get h5p iframe
        h5p_iframe = html.find("iframe")
        iframe_src_value = h5p_iframe.get("src") if h5p_iframe else None
        if iframe_src_value:
            iframe_src = urllib.parse.urljoin(html_url, cast(str, iframe_src_value))
            iframe_html = str(
                bs(
                    ctx.session.get(iframe_src).text,
                    features="lxml",
                )
            )
            # Moodle devs dont know how to use CDATA correctly, so we need to remove all backslashes
            sanitized_html = iframe_html.replace("\\", "")
        else:
            # H5P outside iframes
            sanitized_html = str(html).replace("\\", "")

        services.scan_for_links(
            sanitized_html,
            section_node,
            course_id,
            module_title=module["modname"],
            single=False,
        )
    else:
        services.scan_for_links(
            module.get("description", ""),
            section_node,
            course_id,
            module_title=module["name"],
        )


def handle_opencast_lti_module(
    ctx,
    module,
    section_node,
    course_node,
    services: ModuleServices,
    log: logging.Logger = logger,
) -> None:
    # New OpenCast integration
    if module["modname"] != "lti" or not ctx.config.get("used_modules", {}).get(
        "url", {}
    ).get("opencast", {}):
        return

    info_url = (
        f'https://moodle.rwth-aachen.de/mod/lti/launch.php?id={module["id"]}'
        "&triggerview=0"
    )
    try:
        info_response = ctx.session.get(info_url)
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
        services.log_opencast_backend_issue(info_response.text)
        return

    info_res = bs(info_response.text, features="lxml")

    engage_series_id = services.get_input_value(info_res, "custom_series")
    engage_single_id = services.get_input_value(info_res, "custom_id")
    name = services.get_input_value(info_res, "resource_link_title") or module["name"]
    engage_data = services.extract_lti_form_data(info_res)

    if engage_series_id:
        # Found an Opencast "series" page
        series_id = engage_series_id

        series_node = course_node.add_child(name, series_id, "Section")

        if not services.submit_opencast_lti_form(
            engage_data, f"LTI series module {module['id']}"
        ):
            return

        series_url = (
            "https://engage.streaming.rwth-aachen.de/search/episode.json"
            f"?limit=100&offset=0&sid={series_id}"
        )
        series_response = services.fetch_opencast_json(
            series_url, f"series {series_id}"
        )
        if series_response is None:
            return

        for episode in services.get_opencast_result_list(
            series_response, f"series {series_id}"
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
            vid = services.extract_track_from_episode(episode_id)
            if not vid:
                continue
            if services.should_skip_url(vid, "Opencast video URL"):
                continue
            series_node.add_child(
                mediapackage.get("title") or episode_id,
                episode_id,
                "Opencast",
                url=vid,
                additional_info=module["id"],
            )
    else:
        if not engage_single_id:
            log.info("Failed to find either custom_id or custom_series on lti page.")
            log.info("------LTI-ERROR-HTML------")
            log.info(f"url: {info_url}")
            log.info(info_res)
        else:
            if not services.submit_opencast_lti_form(
                engage_data, f"LTI module {module['id']}"
            ):
                return
            vid = services.extract_track_from_episode(engage_single_id)
            if not vid:
                return
            if services.should_skip_url(vid, "Opencast video URL"):
                return
            section_node.add_child(
                name,
                engage_single_id,
                "Opencast",
                url=vid,
                additional_info=module["id"],
            )
