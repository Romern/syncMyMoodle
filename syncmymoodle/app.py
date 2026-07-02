import json
import logging
from pathlib import Path

from syncmymoodle import downloader
from syncmymoodle import links as links_api
from syncmymoodle import moodle as moodle_api
from syncmymoodle import moodle_files
from syncmymoodle import opencast as opencast_api
from syncmymoodle import sync_handlers
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
        return moodle_files.get_or_add_child(parent_node, name, id, type)

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
        return moodle_files.add_moodle_file_node(
            parent_node,
            self.invalid_chars,
            moodle_filepath,
            filename,
            id,
            type,
            url,
            timemodified=timemodified,
            name_clash_id=name_clash_id,
        )

    def _add_moodle_content_file_node(self, parent_node, content, file_type=None):
        return moodle_files.add_moodle_content_file_node(
            parent_node, self.invalid_chars, content, file_type
        )

    def _is_direct_moodle_file_content(self, module, content):
        return moodle_files.is_direct_moodle_file_content(module, content)

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
        return downloader.local_file_matches_etag(path, etag)

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
            authenticate_opencast_episode=self._authenticate_opencast_episode,
            extract_lti_form_data=self._extract_lti_form_data,
            extract_opencast_episode_id=self._extract_opencast_episode_id,
            extract_track_from_episode=self.extractTrackFromEpisode,
            fetch_opencast_json=self._fetch_opencast_json,
            get_input_value=self._get_input_value,
            get_opencast_result_list=self._get_opencast_result_list,
            is_direct_moodle_file_content=self._is_direct_moodle_file_content,
            log_opencast_backend_issue=self._log_opencast_backend_issue,
            sanitize=self.sanitize,
            scan_html_text_for_links=self._scan_html_text_for_links,
            scan_for_links=self.scanForLinks,
            should_skip_url=self._should_skip_url,
            submit_opencast_lti_form=self._submit_opencast_lti_form,
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
                module_context = sync_handlers.ModuleContext(
                    ctx=self.ctx,
                    course_id=course_id,
                    course_node=course_node,
                    section_node=section_node,
                    assignments_by_cmid=assignments_by_cmid,
                    folders_by_coursemodule=folders_by_coursemodule,
                    services=module_services,
                    log=logger,
                )
                for module in section["modules"]:
                    try:
                        if self._should_skip_module(module, course_id):
                            continue

                        sync_handlers.handle_module(module_context, module)

                    except Exception:
                        logger.exception(f"Failed to download the module {module}")

        self.root_node.remove_children_nameclashes()

    def download_all_files(self):
        return downloader.download_all_files(
            self.ctx,
            downloader.DownloadTreeServices(
                download_file=self.download_file,
                scan_and_download_youtube=self.scanAndDownloadYouTube,
            ),
            logger,
        )

    def _download_all_files(self, cur_node):
        return downloader.download_node_tree(
            cur_node,
            downloader.DownloadTreeServices(
                download_file=self.download_file,
                scan_and_download_youtube=self.scanAndDownloadYouTube,
            ),
            logger,
        )

    def get_sanitized_node_path(self, node: Node) -> Path:
        return get_sanitized_node_path(
            node, Path(self.config.get("basedir", "./")), self.invalid_chars
        )

    def sanitize(self, path):
        return sanitize_path_part(path, self.invalid_chars)

    def _content_type_without_parameters(self, response):
        return downloader.content_type_without_parameters(response)

    def _node_allows_html_download(self, node):
        return downloader.node_allows_html_download(node)

    def _chunk_looks_like_html(self, chunk):
        return downloader.chunk_looks_like_html(chunk)

    def _download_response_is_usable(self, node, response, downloadpath):
        return downloader.download_response_is_usable(
            node, response, downloadpath, logger
        )

    def download_file(self, node):
        return downloader.download_file(
            self.ctx,
            node,
            downloader.DownloadServices(
                chunk_looks_like_html=self._chunk_looks_like_html,
                download_response_is_usable=self._download_response_is_usable,
                get_old_node_for=self._get_old_node_for,
                get_sanitized_node_path=self.get_sanitized_node_path,
                local_file_matches_etag=self._local_file_matches_etag,
                make_conflict_path=self._make_conflict_path,
                node_allows_html_download=self._node_allows_html_download,
                should_skip_url=self._should_skip_url,
            ),
            self.block_size,
            logger,
        )

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
        return downloader.scan_and_download_youtube(
            node, self.get_sanitized_node_path, self._should_skip_url
        )

    def downloadQuiz(self, node):
        return downloader.download_quiz(node, logger)

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
