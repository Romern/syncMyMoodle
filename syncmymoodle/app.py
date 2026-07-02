import hashlib
import json
import logging
import os
import re
import urllib.parse
from contextlib import closing
from fnmatch import fnmatchcase
from pathlib import Path

import yt_dlp
from bs4 import BeautifulSoup as bs
from tqdm import tqdm

from syncmymoodle import links as links_api
from syncmymoodle import moodle as moodle_api
from syncmymoodle import opencast as opencast_api
from syncmymoodle import sync_handlers
from syncmymoodle.constants import YOUTUBE_ID_LENGTH
from syncmymoodle.context import SyncContext
from syncmymoodle.course_cache import (
    cache_root_node,
    ensure_timemodified_attribute,
    get_course_cache_root,
    get_course_node,
    get_old_node_for,
    match_old_cache_child,
    node_from_cache_data,
    node_to_cache_data,
)
from syncmymoodle.filters import (
    as_list,
    configured_patterns,
    course_id_in_filter,
    domain_matches,
    format_course_name,
    matches_any_pattern,
    should_skip_module,
    should_skip_section,
    should_skip_url,
)
from syncmymoodle.node import NAME_CLASH_ID_UNSET, Node
from syncmymoodle.pathing import (
    get_sanitized_node_path,
    make_conflict_path,
    sanitize_path_part,
)
from syncmymoodle.rwth import (
    check_general_connectivity,
    check_moodle_availability,
    check_rwth_status_page,
    current_rwth_service_issues,
)
from syncmymoodle.rwth import login as rwth_login

logger = logging.getLogger(__name__)


class SyncMyMoodle:
    params = {"lang": "en"}  # Titles for some pages differ
    block_size = 1024
    invalid_chars = '~"#%&*:<>?/\\{|}'

    def __init__(self, config):
        self.ctx = SyncContext(config=config)

    @property
    def config(self):
        return self.ctx.config

    @config.setter
    def config(self, value):
        self.ctx.config = value

    @property
    def session(self):
        return self.ctx.session

    @session.setter
    def session(self, value):
        self.ctx.session = value

    @property
    def session_key(self):
        return self.ctx.session_key

    @session_key.setter
    def session_key(self, value):
        self.ctx.session_key = value

    @property
    def wstoken(self):
        return self.ctx.wstoken

    @wstoken.setter
    def wstoken(self, value):
        self.ctx.wstoken = value

    @property
    def user_id(self):
        return self.ctx.user_id

    @user_id.setter
    def user_id(self, value):
        self.ctx.user_id = value

    @property
    def user_private_access_key(self):
        return self.ctx.user_private_access_key

    @user_private_access_key.setter
    def user_private_access_key(self, value):
        self.ctx.user_private_access_key = value

    @property
    def root_node(self):
        return self.ctx.root_node

    @root_node.setter
    def root_node(self, value):
        self.ctx.root_node = value

    @property
    def _course_caches(self):
        return self.ctx.course_caches

    @_course_caches.setter
    def _course_caches(self, value):
        self.ctx.course_caches = value

    @property
    def _opencast_error_count(self):
        return self.ctx.opencast_error_count

    @_opencast_error_count.setter
    def _opencast_error_count(self, value):
        self.ctx.opencast_error_count = value

    @property
    def _opencast_status_hint_logged(self):
        return self.ctx.opencast_status_hint_logged

    @_opencast_status_hint_logged.setter
    def _opencast_status_hint_logged(self, value):
        self.ctx.opencast_status_hint_logged = value

    @property
    def _sciebo_link_cache(self):
        return self.ctx.sciebo_link_cache

    @_sciebo_link_cache.setter
    def _sciebo_link_cache(self, value):
        self.ctx.sciebo_link_cache = value

    @property
    def _opencast_episode_auth_cache(self):
        return self.ctx.opencast_episode_auth_cache

    @_opencast_episode_auth_cache.setter
    def _opencast_episode_auth_cache(self, value):
        self.ctx.opencast_episode_auth_cache = value

    @property
    def _opencast_track_cache(self):
        return self.ctx.opencast_track_cache

    @_opencast_track_cache.setter
    def _opencast_track_cache(self, value):
        self.ctx.opencast_track_cache = value

    @property
    def _downloaded_paths(self):
        if self.ctx.downloaded_paths is None:
            raise AttributeError("_downloaded_paths")
        return self.ctx.downloaded_paths

    @_downloaded_paths.setter
    def _downloaded_paths(self, value):
        self.ctx.downloaded_paths = value

    def _match_old_cache_child(self, old_node, child):
        return match_old_cache_child(old_node, child)

    def _node_to_cache_data(self, node: Node, old_node: Node | None = None):
        return node_to_cache_data(self.ctx, self.invalid_chars, node, old_node)

    def _node_from_cache_data(self, data, parent=None):
        return node_from_cache_data(data, parent)

    def cache_root_node(self):
        return cache_root_node(self.ctx, self.invalid_chars, logger)

    def _ensure_timemodified_attribute(self, node):
        return ensure_timemodified_attribute(node)

    def _get_course_node(self, node: Node) -> Node:
        return get_course_node(node)

    def _get_course_cache_root(self, course_node: Node):
        return get_course_cache_root(self.ctx, self.invalid_chars, course_node, logger)

    def _get_old_node_for(self, node: Node):
        return get_old_node_for(self.ctx, self.invalid_chars, node, logger)

    def _get_or_add_child(self, parent_node, name, id, type):
        for child in parent_node.children:
            if child.name == name and child.type == type:
                return child
        return parent_node.add_child(name, id, type)

    def _add_moodle_file_node(
        self,
        parent_node,
        moodle_filepath,
        filename,
        id,
        type,
        url,
        timemodified=None,
        name_clash_id=NAME_CLASH_ID_UNSET,
    ):
        target_node = parent_node
        path_segments = [
            self.sanitize(segment)
            for segment in str(moodle_filepath or "").strip("/").split("/")
            if segment
        ]

        for segment in path_segments:
            target_node = self._get_or_add_child(target_node, segment, None, "Folder")
            if target_node is None:
                return None

        return target_node.add_child(
            filename,
            id,
            type,
            url=url,
            timemodified=timemodified,
            name_clash_id=name_clash_id,
        )

    def _add_moodle_content_file_node(self, parent_node, content, file_type=None):
        file_url = content.get("fileurl")
        if not file_url:
            return None

        mimetype = content.get("mimetype") or "unknown"
        filename = urllib.parse.urlsplit(file_url).path.split("/")[-1]
        if not filename:
            filename = content.get("filename")
        return self._add_moodle_file_node(
            parent_node,
            "/",
            filename,
            file_url,
            file_type or f"Linked file [{mimetype}]",
            file_url,
            timemodified=content.get("timemodified"),
            name_clash_id=None,
        )

    def _is_direct_moodle_file_content(self, module, content):
        file_url = content.get("fileurl")
        if not file_url or content.get("type") != "file":
            return False

        mimetype = str(content.get("mimetype") or "").split(";", 1)[0].lower()
        if not mimetype or mimetype in {
            "document/unknown",
            "unknown",
            "text/html",
            "application/xhtml+xml",
        }:
            return False
        if mimetype.startswith("text/"):
            return False

        modname = module.get("modname")
        if modname in {"resource", "pdfannotator"}:
            return True

        # Page modules often expose their rendered body as index.html. Keep
        # that path in the HTML scanner, but direct-add binary attachments.
        if modname == "page" and content.get("filename") != "index.html":
            return True

        return False

    def _scan_html_text_for_links(
        self, html_text, base_url, parent_node, course_id, module_title=None
    ):
        return links_api.scan_html_text_for_links(
            html_text,
            base_url,
            parent_node,
            course_id,
            module_title,
            self._should_skip_url,
            self.scanForLinks,
            logger,
        )

    def _as_list(self, value):
        return as_list(value)

    def _course_id_in_filter(self, course_id, entries) -> bool:
        return course_id_in_filter(course_id, entries)

    def _configured_patterns(self, *keys, course_id=None):
        return configured_patterns(self.config, *keys, course_id=course_id)

    def _format_course_name(self, course_name):
        return format_course_name(course_name, self.config, logger)

    def _matches_any_pattern(self, values, patterns):
        return matches_any_pattern(values, patterns)

    def _domain_matches(self, netloc, allowed_domain):
        return domain_matches(netloc, allowed_domain)

    def _should_skip_url(self, url, context="link"):
        return should_skip_url(self.config, url, context, logger)

    def _should_skip_section(self, section, course_id):
        return should_skip_section(self.config, section, course_id, logger)

    def _should_skip_module(self, module, course_id):
        return should_skip_module(self.config, module, course_id, logger)

    def _make_conflict_path(self, path: Path) -> Path:
        return make_conflict_path(path)

    def _local_file_matches_etag(self, path: Path, etag: str) -> bool:
        """Return True if the local file content matches the given ETag hash.

        We currently support strong ETags that contain a plain hex digest for
        MD5 (32 chars), SHA1 (40 chars) or SHA256 (64 chars). Other formats are
        ignored and treated as non-matching.
        """
        # Extract a plausible hex digest from the ETag value, ignoring weak
        # prefixes (W/) and surrounding quotes or algorithm markers.
        match = re.search(r"([0-9a-fA-F]{32,64})", etag)
        if not match:
            return False
        hex_str = match.group(1).lower()

        algo = None
        if len(hex_str) == 32:
            algo = "md5"
        elif len(hex_str) == 40:
            algo = "sha1"
        elif len(hex_str) == 64:
            algo = "sha256"
        else:
            return False

        with path.open("rb") as f:
            digest = hashlib.file_digest(f, algo)
            return digest.hexdigest() == hex_str

    def _log_opencast_backend_issue(self, response_body: str | None = None) -> None:
        return opencast_api.log_backend_issue(self.ctx, response_body, logger)

    def _check_general_connectivity(self):
        return check_general_connectivity(logger)

    def _current_rwth_service_issues(self, service_name, status_url):
        return current_rwth_service_issues(service_name, status_url, logger)

    def _check_rwth_status_page(self):
        return check_rwth_status_page(logger)

    def _check_moodle_availability(self):
        return check_moodle_availability(self.session, logger)

    # RWTH SSO Login

    def login(self):
        return rwth_login(self.ctx, logger)

    # Moodle Web Services API

    def get_moodle_wstoken(self):
        self.wstoken = moodle_api.get_moodle_wstoken(self.session, logger)
        return self.wstoken

    def get_all_courses(self):
        return moodle_api.get_all_courses(self.session, self.wstoken, self.user_id)

    def get_course(self, course_id):
        return moodle_api.get_course(self.session, self.wstoken, course_id)

    def get_userid(self):
        self.user_id, self.user_private_access_key = moodle_api.get_userid(
            self.session, self.wstoken, logger
        )
        return self.user_id, self.user_private_access_key

    def get_assignment(self, course_id):
        return moodle_api.get_assignment(self.session, self.wstoken, course_id)

    def get_assignment_submission_files(self, assignment_id):
        return moodle_api.get_assignment_submission_files(
            self.session, self.wstoken, self.user_id, assignment_id, logger
        )

    def get_folders_by_courses(self, course_id):
        return moodle_api.get_folders_by_courses(self.session, self.wstoken, course_id)

    def sync(self):
        """Retrives the file tree for all courses"""
        if not self.session:
            raise Exception("You need to login() first.")
        if not self.wstoken:
            raise Exception("You need to get_moodle_wstoken() first.")
        if not self.user_id:
            raise Exception("You need to get_userid() first.")
        self.root_node = Node("", -1, "Root", None)
        module_services = sync_handlers.ModuleServices(
            add_moodle_file_node=self._add_moodle_file_node,
            add_moodle_content_file_node=self._add_moodle_content_file_node,
            get_assignment_submission_files=self.get_assignment_submission_files,
            is_direct_moodle_file_content=self._is_direct_moodle_file_content,
            scan_for_links=self.scanForLinks,
            should_skip_url=self._should_skip_url,
        )

        # Syncing all courses
        for course in self.get_all_courses():
            course_name = self._format_course_name(
                course.get("shortname") or f"course-{course.get('id')}"
            )
            course_id = course["id"]

            selected_courses = self.config.get("selected_courses", [])
            if selected_courses:
                # selected_courses is an explicit allowlist that overrides
                # skip_courses (and, below, only_sync_semester).
                if not self._course_id_in_filter(course_id, selected_courses):
                    continue
            elif self._course_id_in_filter(
                course_id, self.config.get("skip_courses", [])
            ):
                continue

            semestername = (course.get("idnumber") or "")[:4] or "unknown-semester"
            # Skip not selected semesters (selected_courses overrides this)
            if (
                not selected_courses
                and self.config.get("only_sync_semester", [])
                and semestername not in self.config.get("only_sync_semester", [])
            ):
                continue

            semester_node = [
                s for s in self.root_node.children if s.name == semestername
            ]
            if len(semester_node) == 0:
                semester_node = self.root_node.add_child(semestername, None, "Semester")
            else:
                semester_node = semester_node[0]

            course_node = semester_node.add_child(course_name, course_id, "Course")

            print(f"Syncing {course_name}...")
            course_sections = self.get_course(course_id)
            module_names = {
                module.get("modname")
                for section in course_sections
                if isinstance(section, dict)
                for module in section.get("modules", [])
            }

            assignments = None
            if self.config.get("used_modules", {}).get("assign", {}) and (
                "assign" in module_names
            ):
                assignments = self.get_assignment(course_id)
            assignments_by_cmid = {
                assignment["cmid"]: assignment
                for assignment in ((assignments or {}).get("assignments") or [])
                if "cmid" in assignment
            }

            folders = []
            if self.config.get("used_modules", {}).get("folder", {}) and (
                "folder" in module_names
            ):
                folders = self.get_folders_by_courses(course_id)
            folders_by_coursemodule = {
                folder.get("coursemodule"): folder for folder in folders
            }

            logger.info("-----------------------")
            logger.info(f"------{semestername} - {course_name}------")
            logger.info("------COURSE-DATA------")
            logger.info(json.dumps(course))
            logger.info("------ASSIGNMENT-DATA------")
            logger.info(json.dumps(assignments))
            logger.info("------FOLDER-DATA------")
            logger.info(json.dumps(folders))

            for section in course_sections:
                if isinstance(section, str):
                    logger.error(f"Error syncing section in {course_name}: {section}")
                    continue
                if self._should_skip_section(section, course_id):
                    continue
                logger.info("------SECTION-DATA------")
                logger.info(json.dumps(section))
                section_node = course_node.add_child(
                    section["name"], section["id"], "Section"
                )
                for module in section["modules"]:
                    try:
                        if self._should_skip_module(module, course_id):
                            continue

                        sync_handlers.handle_assignment_module(
                            self.ctx,
                            module,
                            section_node,
                            course_id,
                            assignments_by_cmid,
                            module_services,
                        )
                        sync_handlers.handle_resource_like_module(
                            self.ctx,
                            module,
                            section_node,
                            course_id,
                            module_services,
                        )
                        sync_handlers.handle_folder_module(
                            self.ctx,
                            module,
                            section_node,
                            course_id,
                            folders_by_coursemodule,
                            module_services,
                        )

                        # Get embedded videos in pages or labels
                        if module["modname"] in [
                            "page",
                            "label",
                            "h5pactivity",
                        ] and self.config.get("used_modules", {}).get("url", {}):
                            if module["modname"] == "page":
                                opencast_enabled = (
                                    self.config.get("used_modules", {})
                                    .get("url", {})
                                    .get("opencast", {})
                                )
                                html_url = (
                                    module.get("url")
                                    or f'https://moodle.rwth-aachen.de/mod/page/view.php?id={module["id"]}'
                                )
                                scan_page_links = not self.config.get(
                                    "nolinks"
                                ) and not self._should_skip_url(html_url, "page link")
                                if opencast_enabled or scan_page_links:
                                    try:
                                        response = self.session.get(html_url)
                                    except Exception:
                                        logger.exception(
                                            "Failed to fetch page module %s",
                                            module["id"],
                                        )
                                        response = None
                                    if response and not (
                                        200 <= response.status_code < 300
                                    ):
                                        logger.warning(
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
                                                iframe_src = iframe.get("src")
                                                if not iframe_src:
                                                    continue
                                                iframe_src = urllib.parse.urljoin(
                                                    response.url or html_url,
                                                    iframe_src,
                                                )
                                                vid_id = (
                                                    self._extract_opencast_episode_id(
                                                        iframe_src
                                                    )
                                                )
                                                if not vid_id:
                                                    continue
                                                if not self._authenticate_opencast_episode(
                                                    course_id, vid_id
                                                ):
                                                    continue
                                                vid = self.extractTrackFromEpisode(
                                                    vid_id
                                                )
                                                if not vid:
                                                    continue

                                                if self._should_skip_url(
                                                    vid, "Opencast video URL"
                                                ):
                                                    continue

                                                section_node.add_child(
                                                    module["name"],
                                                    vid_id,
                                                    "Opencast",
                                                    url=vid,
                                                    additional_info=course_id,
                                                )

                                        if scan_page_links:
                                            self._scan_html_text_for_links(
                                                response.text,
                                                response.url or html_url,
                                                section_node,
                                                course_id,
                                                module_title=module["name"],
                                            )
                            # "Interactive" h5p videos
                            elif module["modname"] == "h5pactivity":
                                html_url = f'https://moodle.rwth-aachen.de/mod/h5pactivity/view.php?id={module["id"]}'
                                html = bs(
                                    self.session.get(html_url).text,
                                    features="lxml",
                                )
                                # Get h5p iframe
                                iframe = html.find("iframe")
                                iframe_src = iframe.get("src") if iframe else None
                                if iframe_src:
                                    iframe_src = urllib.parse.urljoin(
                                        html_url, iframe_src
                                    )
                                    iframe_html = str(
                                        bs(
                                            self.session.get(iframe_src).text,
                                            features="lxml",
                                        )
                                    )
                                    # Moodle devs dont know how to use CDATA correctly, so we need to remove all backslashes
                                    sanitized_html = iframe_html.replace("\\", "")
                                else:
                                    # H5P outside iframes
                                    sanitized_html = str(html).replace("\\", "")

                                self.scanForLinks(
                                    sanitized_html,
                                    section_node,
                                    course_id,
                                    module_title=module["modname"],
                                    single=False,
                                )
                            else:
                                self.scanForLinks(
                                    module.get("description", ""),
                                    section_node,
                                    course_id,
                                    module_title=module["name"],
                                )

                        # New OpenCast integration
                        if module["modname"] == "lti" and self.config.get(
                            "used_modules", {}
                        ).get("url", {}).get("opencast", {}):
                            info_url = f'https://moodle.rwth-aachen.de/mod/lti/launch.php?id={module["id"]}&triggerview=0'
                            try:
                                info_response = self.session.get(info_url)
                            except Exception:
                                logger.exception(
                                    "Opencast: failed to fetch LTI module %s",
                                    module["id"],
                                )
                                continue
                            if not (200 <= info_response.status_code < 300):
                                logger.warning(
                                    "Opencast: LTI module %s returned status %s",
                                    module["id"],
                                    info_response.status_code,
                                )
                                self._log_opencast_backend_issue(info_response.text)
                                continue

                            info_res = bs(info_response.text, features="lxml")

                            engage_series_id = self._get_input_value(
                                info_res, "custom_series"
                            )
                            engage_single_id = self._get_input_value(
                                info_res, "custom_id"
                            )
                            name = (
                                self._get_input_value(info_res, "resource_link_title")
                                or module["name"]
                            )
                            engage_data = self._extract_lti_form_data(info_res)

                            if engage_series_id:
                                # Found an Opencast "series" page
                                series_id = engage_series_id

                                series_node = course_node.add_child(
                                    name, series_id, "Section"
                                )

                                if not self._submit_opencast_lti_form(
                                    engage_data, f"LTI series module {module['id']}"
                                ):
                                    continue

                                series_url = f"https://engage.streaming.rwth-aachen.de/search/episode.json?limit=100&offset=0&sid={series_id}"
                                series_response = self._fetch_opencast_json(
                                    series_url, f"series {series_id}"
                                )
                                if series_response is None:
                                    continue

                                for episode in self._get_opencast_result_list(
                                    series_response, f"series {series_id}"
                                ):
                                    if not isinstance(episode, dict):
                                        continue
                                    mediapackage = episode.get("mediapackage", {})
                                    if not isinstance(mediapackage, dict):
                                        continue
                                    episode_id = mediapackage.get("id")
                                    if not episode_id:
                                        logger.warning(
                                            "Opencast: series %s contains episode without id",
                                            series_id,
                                        )
                                        continue
                                    vid = self.extractTrackFromEpisode(episode_id)
                                    if not vid:
                                        continue
                                    if self._should_skip_url(vid, "Opencast video URL"):
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
                                    logger.info(
                                        "Failed to find either custom_id or custom_series on lti page."
                                    )
                                    logger.info("------LTI-ERROR-HTML------")
                                    logger.info(f"url: {info_url}")
                                    logger.info(info_res)
                                else:
                                    if not self._submit_opencast_lti_form(
                                        engage_data, f"LTI module {module['id']}"
                                    ):
                                        continue
                                    vid = self.extractTrackFromEpisode(engage_single_id)
                                    if not vid:
                                        continue
                                    if self._should_skip_url(vid, "Opencast video URL"):
                                        continue
                                    section_node.add_child(
                                        name,
                                        engage_single_id,
                                        "Opencast",
                                        url=vid,
                                        additional_info=module["id"],
                                    )
                        # Integration for Quizzes
                        if module["modname"] == "quiz" and self.config.get(
                            "used_modules", {}
                        ).get("url", {}).get("quiz", {}):
                            info_url = f'https://moodle.rwth-aachen.de/mod/quiz/view.php?id={module["id"]}'
                            info_res = bs(
                                self.session.get(info_url).text, features="lxml"
                            )
                            attempts = info_res.find_all(
                                "a",
                                {
                                    "title": "Überprüfung der eigenen Antworten dieses Versuchs"
                                },
                            )
                            attempt_cnt = 0
                            for attempt in attempts:
                                attempt_cnt += 1
                                review_url = attempt.get("href")
                                quiz_res = bs(
                                    self.session.get(review_url).text,
                                    features="lxml",
                                )
                                name = (
                                    quiz_res.find("title")
                                    .get_text()
                                    .replace(": Überprüfung des Testversuchs", "")
                                    + ", Versuch "
                                    + str(attempt_cnt)
                                )
                                section_node.add_child(
                                    self.sanitize(name),
                                    urllib.parse.urlparse(review_url)[1],
                                    "Quiz",
                                    url=review_url,
                                )

                    except Exception:
                        logger.exception(f"Failed to download the module {module}")

        self.root_node.remove_children_nameclashes()

    def download_all_files(self):
        if not self.session:
            raise Exception("You need to login() first.")
        if not self.wstoken:
            raise Exception("You need to get_moodle_wstoken() first.")
        if not self.user_id:
            raise Exception("You need to get_userid() first.")
        if not self.root_node:
            raise Exception("You need to sync() first.")

        self._download_all_files(self.root_node)

    def _download_all_files(self, cur_node):
        if len(cur_node.children) == 0:
            if cur_node.url and not cur_node.is_downloaded:
                if cur_node.type == "Youtube":
                    try:
                        self.scanAndDownloadYouTube(cur_node)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                        logger.error(
                            "This could be caused by an out of date yt-dlp version. Try upgrading yt-dlp through pip or your package manager."
                        )
                elif cur_node.type == "Opencast":
                    try:
                        # download Opencast videos
                        if ".mp4" not in cur_node.name:
                            if cur_node.name is not None and cur_node.name != "":
                                cur_node.name += ".mp4"
                            else:
                                cur_node.name = cur_node.url.split("/")[-1]
                        if self.download_file(cur_node):
                            cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                elif cur_node.type == "Quiz":
                    logger.warning(
                        "Skipping quiz PDF generation for %s because it is disabled "
                        "for security.",
                        cur_node.name,
                    )
                else:
                    try:
                        if self.download_file(cur_node):
                            cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
            return

        for child in cur_node.children:
            self._download_all_files(child)

    def get_sanitized_node_path(self, node: Node) -> Path:
        return get_sanitized_node_path(
            node, Path(self.config.get("basedir", "./")), self.invalid_chars
        )

    def sanitize(self, path):
        return sanitize_path_part(path, self.invalid_chars)

    def _content_type_without_parameters(self, response):
        content_type = response.headers.get("Content-Type", "")
        return content_type.split(";", 1)[0].strip().lower()

    def _node_allows_html_download(self, node):
        html_suffixes = {".htm", ".html", ".xhtml"}
        node_suffix = Path(str(node.name or "")).suffix.lower()
        url_suffix = Path(
            urllib.parse.urlparse(str(node.url or "")).path
        ).suffix.lower()
        return node_suffix in html_suffixes or url_suffix in html_suffixes

    def _chunk_looks_like_html(self, chunk):
        body_start = chunk.lstrip().lower()
        return body_start.startswith(b"<!doctype html") or body_start.startswith(
            b"<html"
        )

    def _download_response_is_usable(self, node, response, downloadpath):
        if response.status_code == 204:
            logger.warning(
                "Skipping download of %s from %s because the server returned no "
                "content",
                downloadpath,
                node.url,
            )
            return False

        if not (200 <= response.status_code < 300):
            logger.warning(
                "Skipping download of %s from %s because the server returned "
                "HTTP %s",
                downloadpath,
                node.url,
                response.status_code,
            )
            return False

        content_type = self._content_type_without_parameters(response)
        if content_type in {"text/html", "application/xhtml+xml"}:
            if not self._node_allows_html_download(node):
                logger.warning(
                    "Skipping download of %s from %s because the server returned "
                    "HTML instead of the expected file. This usually means the "
                    "link requires a separate login or points to an error page.",
                    downloadpath,
                    node.url,
                )
                return False

        return True

    def download_file(self, node):
        """Download file with progress bar if it isn't already downloaded"""
        downloadpath = self.get_sanitized_node_path(node)

        if self._should_skip_url(node.url, f"{node.type} file"):
            return True

        # Respect filetype/name exclusions up front so that excluded files never
        # trigger conflict handling, displace local files, or create temp files.
        if node.name.split(".")[-1] in self.config.get("exclude_filetypes", []):
            return True
        if any(
            fnmatchcase(node.name, pattern)
            for pattern in self.config.get("exclude_files", [])
        ):
            return True

        # If we already downloaded this path during the current run, skip any
        # further processing. This avoids duplicate downloads and spurious
        # conflicts when the same remote file appears multiple times in the
        # node tree (e.g. Sciebo links reused in a course).
        if hasattr(self, "_downloaded_paths"):
            if downloadpath in self._downloaded_paths:
                return True
        else:
            # Initialise on first use to keep __init__ simple.
            self._downloaded_paths = set()

        # Decide whether we need to (re-)download the file at all
        cached_timemodified = None
        old_node = None
        conflict_rename_pending = False
        if downloadpath.exists():
            if not self.config.get("updatefiles"):
                return True

            # Try to find a cached node for this file from the per-course cache.
            old_node = self._get_old_node_for(node)
            # Only trust the cached version markers when the previous run
            # actually downloaded the file. Otherwise an update that failed last
            # time (e.g. an expired session) gets cached with Moodle's new
            # timemodified and would be skipped forever, leaving a stale file.
            # Treat a non-downloaded cache entry as if there were no cache at all.
            if old_node is not None and not getattr(old_node, "is_downloaded", False):
                old_node = None
            if old_node is not None:
                cached_timemodified = getattr(old_node, "timemodified", None)
                old_etag = getattr(old_node, "etag", None)
                # If Moodle did not change the file, skip re-download. Only when
                # timemodified is meaningful: Sciebo files have no timemodified
                # (always None), so this must fall through to the etag check
                # below instead of treating None == None as "unchanged".
                if cached_timemodified is not None and (
                    node.timemodified == cached_timemodified
                ):
                    return True
                # For Sciebo, we use the etag from the previous run as the
                # remote version marker. If it matches the current etag from
                # the PROPFIND response, the remote file has not changed.
                if (
                    cached_timemodified is None
                    and old_etag
                    and getattr(node, "etag", None) == old_etag
                ):
                    # Additionally, on the first run with a cache, the local file
                    # may already match this etag (e.g. previously downloaded
                    # manually). If so, we can safely skip any download.
                    if self._local_file_matches_etag(downloadpath, old_etag):
                        return True

            # At this point, either there is no cache for this course/path, or
            # Moodle reports a different modification time. This means the
            # remote file might have changed.

            # Check for potential local modifications since the last sync to avoid
            # silently overwriting user changes.
            conflict_mode = self.config.get("update_files_conflict", "rename")
            if conflict_mode not in {"rename", "keep", "none", "overwrite"}:
                conflict_mode = "rename"

            local_conflict = False
            old_etag = getattr(old_node, "etag", None) if old_node is not None else None
            etag_check_failed = False
            if old_etag:
                # Prefer using the old ETag (hash) to detect whether the local file
                # still matches the previously downloaded version.
                try:
                    if not self._local_file_matches_etag(downloadpath, old_etag):
                        local_conflict = True
                except Exception:
                    # A faulty/unusable ETag cache is treated as if we had no
                    # cached ETag at all: fall back to the timestamp/HEAD
                    # heuristic below to decide whether this is a conflict.
                    etag_check_failed = True

            if not old_etag or etag_check_failed:
                if cached_timemodified is not None:
                    # Fallback: compare local mtime with the previous Moodle timestamp.
                    try:
                        local_mtime = int(downloadpath.stat().st_mtime)
                        if local_mtime != int(cached_timemodified):
                            local_conflict = True
                    except (OSError, ValueError):
                        local_conflict = True
                else:
                    # No previous etag and no previous timemodified: this usually
                    # means the file existed before we ever cached it. Before we
                    # treat this as a conflict, try to see if the local file
                    # already matches the *current* remote content using the
                    # ETag from either the Sciebo PROPFIND or a Moodle HEAD
                    # request.
                    remote_etag = getattr(node, "etag", None)
                    if remote_etag is None and node.url:
                        try:
                            head_resp = self.session.head(
                                node.url, allow_redirects=True
                            )
                            remote_etag = head_resp.headers.get("ETag")
                        except Exception:
                            remote_etag = None

                    if remote_etag and self._local_file_matches_etag(
                        downloadpath, remote_etag
                    ):
                        # Local file already equals the current remote content,
                        # so there is no conflict and no need to download again.
                        node.etag = remote_etag
                        if getattr(node, "timemodified", None) is not None:
                            try:
                                ts = int(node.timemodified)
                                os.utime(downloadpath, (ts, ts))
                            except (OSError, OverflowError, ValueError):
                                pass
                        return True

                    # At this point we know the local file differs from the
                    # current remote version (or we couldn't verify), and we
                    # have no prior cached state. Treat this as a potential
                    # conflict to avoid silently overwriting user changes.
                    local_conflict = True

            if local_conflict:
                if conflict_mode in {"keep", "none"}:
                    # Keep the locally modified file and skip updating from Moodle
                    logger.info(
                        "Detected local changes for %s, skipping Moodle update "
                        "due to update_files_conflict=%s",
                        downloadpath,
                        conflict_mode,
                    )
                    return True
                if conflict_mode == "rename":
                    # Defer moving the locally modified file aside until the
                    # replacement has been fully downloaded, so an aborted or
                    # failed download (e.g. an expired session returning an HTML
                    # error page) never leaves the canonical path empty.
                    conflict_rename_pending = True
                # conflict_mode == "overwrite": fall through and overwrite

        # Hidden, namespaced temp/sidecar names so we never resume from or
        # overwrite a file the user happens to own. The sidecar records the
        # ETag a partial download was fetched against.
        tmp_downloadpath = downloadpath.parent / f".{downloadpath.name}.smmpart"
        etag_sidecar = tmp_downloadpath.with_name(tmp_downloadpath.name + ".etag")

        # Only resume a previous partial when we recorded the ETag it was fetched
        # against, so we can ask the server (via If-Range) to confirm the remote
        # content is unchanged. Without that proof a blind range request could
        # splice bytes from a newer version onto an older partial and silently
        # corrupt the file.
        resume_size = 0
        partial_etag: str | None = None
        header = dict()
        if tmp_downloadpath.exists():
            if etag_sidecar.exists():
                try:
                    partial_etag = etag_sidecar.read_text(encoding="utf-8").strip()
                except OSError:
                    partial_etag = None
            if partial_etag:
                resume_size = tmp_downloadpath.stat().st_size
                header = {"Range": f"bytes={resume_size}-", "If-Range": partial_etag}
            else:
                # Cannot validate the partial; discard it and start fresh.
                tmp_downloadpath.unlink(missing_ok=True)
                etag_sidecar.unlink(missing_ok=True)
        if node.type.lower() == "sciebo file":
            header = {**header, **node.additional_info}

        with closing(
            self.session.get(node.url, headers=header, stream=True)
        ) as response:
            etag_header = response.headers.get("ETag")

            if resume_size:
                # The remote content differs from our partial when the server
                # ignores the range (any non-206) or cannot prove that the
                # returned tail belongs to the same ETag as the saved partial.
                valid_resume = (
                    response.status_code == 206 and etag_header == partial_etag
                )
                version_changed = not valid_resume
                if version_changed:
                    resume_size = 0
                    tmp_downloadpath.unlink(missing_ok=True)
                    etag_sidecar.unlink(missing_ok=True)
                    if response.status_code == 206:
                        # This 206 body is only a tail, and without an exact
                        # ETag match it cannot be safely appended. Restart fresh
                        # on the next run.
                        return False

            if not self._download_response_is_usable(node, response, downloadpath):
                return False

            content = response.iter_content(self.block_size)
            first_chunk = next((chunk for chunk in content if chunk), b"")
            if (
                first_chunk
                and self._chunk_looks_like_html(first_chunk)
                and not self._node_allows_html_download(node)
            ):
                logger.warning(
                    "Skipping download of %s from %s because the response body "
                    "starts with HTML instead of the expected file. This usually "
                    "means the link requires a separate login or points to an "
                    "error page.",
                    downloadpath,
                    node.url,
                )
                return False

            print(f"Downloading {downloadpath} [{node.type}]")
            total_size_in_bytes = int(response.headers.get("content-length", 0)) + max(
                resume_size, 0
            )
            progress_bar = tqdm(total=total_size_in_bytes, unit="iB", unit_scale=True)
            if resume_size:
                progress_bar.update(resume_size)
            downloadpath.parent.mkdir(parents=True, exist_ok=True)
            # Record the ETag this partial is being fetched against so an
            # interrupted download can be safely resumed next time.
            if etag_header:
                try:
                    etag_sidecar.write_text(etag_header, encoding="utf-8")
                except OSError:
                    pass
            mode = "ab" if resume_size else "wb"
            with tmp_downloadpath.open(mode) as file:
                if first_chunk:
                    progress_bar.update(len(first_chunk))
                    file.write(first_chunk)
                for data in content:
                    progress_bar.update(len(data))
                    file.write(data)
            progress_bar.close()

            # The replacement is now fully on disk. Only at this point do we move
            # a conflicting local file aside, so a failure above never empties
            # the canonical path.
            if conflict_rename_pending:
                conflict_path = self._make_conflict_path(downloadpath)
                try:
                    downloadpath.rename(conflict_path)
                    logger.warning(
                        "Detected local changes for %s, moved to %s before "
                        "installing the updated file from Moodle",
                        downloadpath,
                        conflict_path,
                    )
                except OSError:
                    logger.exception(
                        "Failed to move locally modified file %s to %s; keeping "
                        "it and discarding the downloaded update to avoid data "
                        "loss",
                        downloadpath,
                        conflict_path,
                    )
                    tmp_downloadpath.unlink(missing_ok=True)
                    etag_sidecar.unlink(missing_ok=True)
                    return True

            os.replace(tmp_downloadpath, downloadpath)
            etag_sidecar.unlink(missing_ok=True)
            # Align the local mtime with Moodle's timemodified to detect local
            # changes on subsequent runs.
            if getattr(node, "timemodified", None) is not None:
                try:
                    ts = int(node.timemodified)
                    os.utime(downloadpath, (ts, ts))
                except (OSError, OverflowError, ValueError):
                    # If updating timestamps fails, fall back to the current time.
                    pass
            # Persist the ETag of the downloaded file on the node so it can be
            # used on the next run to detect local modifications.
            if etag_header is not None:
                try:
                    node.etag = etag_header
                except Exception:
                    # If for some reason we cannot set it, just ignore.
                    pass
            # Remember that we downloaded this path during the current run.
            self._downloaded_paths.add(downloadpath)
            return True

    def _extract_opencast_episode_id(self, url):
        return opencast_api.extract_episode_id(url)

    def _extract_lti_form_data(self, soup):
        return opencast_api.extract_lti_form_data(soup)

    def _get_input_value(self, soup, name):
        return opencast_api.get_input_value(soup, name)

    def _submit_opencast_lti_form(self, engage_data, context):
        return opencast_api.submit_lti_form(self.ctx, engage_data, context, logger)

    def _fetch_lti_form_data(self, url, context):
        return opencast_api.fetch_lti_form_data(self.ctx, url, context, logger)

    def _authenticate_opencast_episode(self, course_id, episode_id):
        return opencast_api.authenticate_episode(
            self.ctx, course_id, episode_id, logger
        )

    def _fetch_opencast_json(self, url, context):
        return opencast_api.fetch_json(self.ctx, url, context, logger)

    def _get_opencast_result_list(self, payload, context):
        return opencast_api.get_result_list(self.ctx, payload, context, logger)

    def _resolution_width(self, resolution):
        return opencast_api.resolution_width(resolution)

    def extractTrackFromEpisode(self, episode_id):
        return opencast_api.extract_track_from_episode(self.ctx, episode_id, logger)

    def scanAndDownloadYouTube(self, node):
        """Download Youtube-Videos using yt_dlp"""
        path = self.get_sanitized_node_path(node.parent)
        link = node.url
        if self._should_skip_url(link, "YouTube link"):
            return True
        if path.exists():
            if any(link[-YOUTUBE_ID_LENGTH:] in f.name for f in path.iterdir()):
                return False
        ydl_opts = {
            "outtmpl": "{}/%(title)s-%(id)s.%(ext)s".format(path),
            "ignoreerrors": True,
            "nooverwrites": True,
            "retries": 15,
            "match_filter": yt_dlp.match_filter_func("!is_live"),
        }
        path.mkdir(parents=True, exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([link])
        return True

    def downloadQuiz(self, node):
        logger.warning(
            "Quiz PDF generation is disabled until the pdfkit/wkhtmltopdf "
            "renderer is replaced with a safer implementation."
        )
        return False

    def scanForLinks(
        self, text, parent_node, course_id, module_title=None, single=False
    ):
        return links_api.scan_for_links(
            self.ctx,
            text,
            parent_node,
            course_id,
            module_title,
            single,
            self._should_skip_url,
            self._content_type_without_parameters,
            self._scan_html_text_for_links,
            self._extract_opencast_episode_id,
            self._authenticate_opencast_episode,
            self.extractTrackFromEpisode,
            logger,
        )
