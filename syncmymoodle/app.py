import json
import logging
from pathlib import Path
from typing import Any, cast

from syncmymoodle import downloader
from syncmymoodle import links as links_api
from syncmymoodle import moodle as moodle_api
from syncmymoodle import sync_handlers
from syncmymoodle.config import Config
from syncmymoodle.constants import INVALID_CHARS
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
    course_id_in_filter,
    format_course_name,
    should_skip_module,
    should_skip_section,
    should_skip_url,
)
from syncmymoodle.node import Node
from syncmymoodle.pathing import get_sanitized_node_path, make_conflict_path
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
    invalid_chars = INVALID_CHARS

    def __init__(self, config: Config | dict[str, Any]) -> None:
        if not isinstance(config, Config):
            config = Config.from_dict(config)
        self.ctx = SyncContext(config=config)

    @property
    def config(self) -> Config:
        return self.ctx.config

    @config.setter
    def config(self, value: Config | dict[str, Any]) -> None:
        if not isinstance(value, Config):
            value = Config.from_dict(value)
        self.ctx.config = value

    @property
    def session(self) -> Any:
        return self.ctx.session

    @session.setter
    def session(self, value: Any) -> None:
        self.ctx.session = value

    @property
    def session_key(self) -> str | None:
        return self.ctx.session_key

    @session_key.setter
    def session_key(self, value: str | None) -> None:
        self.ctx.session_key = value

    @property
    def wstoken(self) -> str | None:
        return self.ctx.wstoken

    @wstoken.setter
    def wstoken(self, value: str | None) -> None:
        self.ctx.wstoken = value

    @property
    def user_id(self) -> Any:
        return self.ctx.user_id

    @user_id.setter
    def user_id(self, value: Any) -> None:
        self.ctx.user_id = value

    @property
    def user_private_access_key(self) -> str | None:
        return self.ctx.user_private_access_key

    @user_private_access_key.setter
    def user_private_access_key(self, value: str | None) -> None:
        self.ctx.user_private_access_key = value

    @property
    def root_node(self) -> Node | None:
        return self.ctx.root_node

    @root_node.setter
    def root_node(self, value: Node | None) -> None:
        self.ctx.root_node = value

    @property
    def _course_caches(self) -> dict[Path, Node]:
        return self.ctx.course_caches

    @_course_caches.setter
    def _course_caches(self, value: dict[Path, Node]) -> None:
        self.ctx.course_caches = value

    @property
    def _opencast_error_count(self) -> int:
        return self.ctx.opencast_error_count

    @_opencast_error_count.setter
    def _opencast_error_count(self, value: int) -> None:
        self.ctx.opencast_error_count = value

    @property
    def _opencast_status_hint_logged(self) -> bool:
        return self.ctx.opencast_status_hint_logged

    @_opencast_status_hint_logged.setter
    def _opencast_status_hint_logged(self, value: bool) -> None:
        self.ctx.opencast_status_hint_logged = value

    @property
    def _sciebo_link_cache(self) -> dict[str, Node]:
        return self.ctx.sciebo_link_cache

    @_sciebo_link_cache.setter
    def _sciebo_link_cache(self, value: dict[str, Node]) -> None:
        self.ctx.sciebo_link_cache = value

    @property
    def _opencast_episode_auth_cache(self) -> set[tuple[Any, str]]:
        return self.ctx.opencast_episode_auth_cache

    @_opencast_episode_auth_cache.setter
    def _opencast_episode_auth_cache(self, value: set[tuple[Any, str]]) -> None:
        self.ctx.opencast_episode_auth_cache = value

    @property
    def _opencast_track_cache(self) -> dict[str, str]:
        return self.ctx.opencast_track_cache

    @_opencast_track_cache.setter
    def _opencast_track_cache(self, value: dict[str, str]) -> None:
        self.ctx.opencast_track_cache = value

    @property
    def _downloaded_paths(self) -> set[Path]:
        if self.ctx.downloaded_paths is None:
            raise AttributeError("_downloaded_paths")
        return self.ctx.downloaded_paths

    @_downloaded_paths.setter
    def _downloaded_paths(self, value: set[Path] | None) -> None:
        self.ctx.downloaded_paths = value

    def _match_old_cache_child(self, old_node: Node | None, child: Node) -> Node | None:
        return match_old_cache_child(old_node, child)

    def _node_to_cache_data(
        self, node: Node, old_node: Node | None = None
    ) -> dict[str, Any]:
        return node_to_cache_data(self.ctx, self.invalid_chars, node, old_node)

    def _node_from_cache_data(
        self, data: dict[str, Any], parent: Node | None = None
    ) -> Node:
        return node_from_cache_data(data, parent)

    def cache_root_node(self) -> None:
        return cache_root_node(self.ctx, self.invalid_chars, logger)

    def _ensure_timemodified_attribute(self, node: Node) -> None:
        return ensure_timemodified_attribute(node)

    def _get_course_node(self, node: Node) -> Node:
        return get_course_node(node)

    def _get_course_cache_root(self, course_node: Node) -> Node | None:
        return get_course_cache_root(self.ctx, self.invalid_chars, course_node, logger)

    def _get_old_node_for(self, node: Node) -> Node | None:
        return get_old_node_for(self.ctx, self.invalid_chars, node, logger)

    def _course_id_in_filter(self, course_id: Any, entries: Any) -> bool:
        return course_id_in_filter(course_id, entries)

    def _format_course_name(self, course_name: str) -> str:
        return format_course_name(course_name, self.config, logger)

    def _should_skip_url(self, url: str | None, context: str = "link") -> bool:
        return should_skip_url(self.config, url, context, logger)

    def _should_skip_section(self, section: dict[str, Any], course_id: Any) -> bool:
        return should_skip_section(self.config, section, course_id, logger)

    def _should_skip_module(self, module: dict[str, Any], course_id: Any) -> bool:
        return should_skip_module(self.config, module, course_id, logger)

    def _make_conflict_path(self, path: Path) -> Path:
        return make_conflict_path(path)

    def _local_file_matches_etag(self, path: Path, etag: str) -> bool:
        return downloader.local_file_matches_etag(path, etag)

    def _check_general_connectivity(self) -> bool:
        return check_general_connectivity(logger)

    def _current_rwth_service_issues(
        self, service_name: str, status_url: str
    ) -> list[dict[str, str]]:
        return current_rwth_service_issues(service_name, status_url, logger)

    def _check_rwth_status_page(self) -> None:
        return check_rwth_status_page(logger)

    def _check_moodle_availability(self) -> Any:
        return check_moodle_availability(self.session, logger)

    # RWTH SSO Login

    def login(self) -> None:
        return rwth_login(self.ctx, logger)

    # Moodle Web Services API

    def get_moodle_wstoken(self) -> str:
        token = moodle_api.get_moodle_wstoken(self.session, logger)
        self.wstoken = token
        return token

    def get_all_courses(self) -> Any:
        return moodle_api.get_all_courses(
            self.ctx.require_session(), cast(str, self.wstoken), self.user_id
        )

    def get_course(self, course_id: Any) -> Any:
        return moodle_api.get_course(
            self.ctx.require_session(), cast(str, self.wstoken), course_id
        )

    def get_userid(self) -> tuple[Any, str]:
        user_id, access_key = moodle_api.get_userid(
            self.ctx.require_session(), cast(str, self.wstoken), logger
        )
        self.user_id = user_id
        self.user_private_access_key = access_key
        return user_id, access_key

    def get_assignment(self, course_id: Any) -> Any:
        return moodle_api.get_assignment(
            self.ctx.require_session(), cast(str, self.wstoken), course_id
        )

    def get_assignment_submission_files(self, assignment_id: Any) -> list[Any]:
        return moodle_api.get_assignment_submission_files(
            self.ctx.require_session(),
            cast(str, self.wstoken),
            self.user_id,
            assignment_id,
            logger,
        )

    def get_folders_by_courses(self, course_id: Any) -> Any:
        return moodle_api.get_folders_by_courses(
            self.ctx.require_session(), cast(str, self.wstoken), course_id
        )

    def sync(self) -> None:
        """Retrieves the file tree for all courses"""
        if not self.session:
            raise Exception("You need to login() first.")
        if not self.wstoken:
            raise Exception("You need to get_moodle_wstoken() first.")
        if not self.user_id:
            raise Exception("You need to get_userid() first.")
        root_node = Node("", -1, "Root", None)
        self.root_node = root_node

        # Syncing all courses
        for course in self.get_all_courses():
            course_name = self._format_course_name(
                course.get("shortname") or f"course-{course.get('id')}"
            )
            course_id = course["id"]

            selected_courses = self.config.selected_courses
            if selected_courses:
                # selected_courses is an explicit allowlist that overrides
                # skip_courses (and, below, only_sync_semester).
                if not self._course_id_in_filter(course_id, selected_courses):
                    continue
            elif self._course_id_in_filter(course_id, self.config.skip_courses):
                continue

            semestername = (course.get("idnumber") or "")[:4] or "unknown-semester"
            # Skip not selected semesters (selected_courses overrides this)
            if (
                not selected_courses
                and self.config.only_sync_semester
                and semestername not in self.config.only_sync_semester
            ):
                continue

            semester_nodes = [s for s in root_node.children if s.name == semestername]
            if len(semester_nodes) == 0:
                semester_node = cast(
                    Node, root_node.add_child(semestername, None, "Semester")
                )
            else:
                semester_node = semester_nodes[0]

            course_node = cast(
                Node, semester_node.add_child(course_name, course_id, "Course")
            )

            print(f"Syncing {course_name}...")
            course_sections = self.get_course(course_id)
            module_names = {
                module.get("modname")
                for section in course_sections
                if isinstance(section, dict)
                for module in section.get("modules", [])
            }

            assignments = None
            if self.config.module_enabled("assign") and ("assign" in module_names):
                assignments = self.get_assignment(course_id)
            assignments_by_cmid = {
                assignment["cmid"]: assignment
                for assignment in ((assignments or {}).get("assignments") or [])
                if "cmid" in assignment
            }

            folders = []
            if self.config.module_enabled("folder") and ("folder" in module_names):
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
                section_node = cast(
                    Node,
                    course_node.add_child(section["name"], section["id"], "Section"),
                )
                module_context = sync_handlers.ModuleContext(
                    ctx=self.ctx,
                    course_id=course_id,
                    course_node=course_node,
                    section_node=section_node,
                    assignments_by_cmid=assignments_by_cmid,
                    folders_by_coursemodule=folders_by_coursemodule,
                    log=logger,
                )
                for module in section["modules"]:
                    try:
                        if self._should_skip_module(module, course_id):
                            continue

                        sync_handlers.handle_module(module_context, module)

                    except Exception:
                        logger.exception(f"Failed to download the module {module}")

        root_node.remove_children_nameclashes()

    def download_all_files(self) -> None:
        return downloader.download_all_files(
            self.ctx,
            downloader.DownloadTreeServices(
                download_file=self.download_file,
                scan_and_download_youtube=self.scanAndDownloadYouTube,
            ),
            logger,
        )

    def _download_all_files(self, cur_node: Node) -> None:
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
            node, Path(self.config.basedir), self.invalid_chars
        )

    def _node_allows_html_download(self, node: Node) -> bool:
        return downloader.node_allows_html_download(node)

    def _chunk_looks_like_html(self, chunk: bytes) -> bool:
        return downloader.chunk_looks_like_html(chunk)

    def _download_response_is_usable(
        self, node: Node, response: Any, downloadpath: Path
    ) -> bool:
        return downloader.download_response_is_usable(
            node, response, downloadpath, logger
        )

    def download_file(self, node: Node) -> bool:
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

    def scanAndDownloadYouTube(self, node: Node) -> bool:
        return downloader.scan_and_download_youtube(
            node, self.get_sanitized_node_path, self._should_skip_url
        )

    def downloadQuiz(self, node: Node) -> bool:
        return downloader.download_quiz(node, logger)

    def scanForLinks(
        self,
        text: str,
        parent_node: Node,
        course_id: Any,
        module_title: Any = None,
        single: bool = False,
    ) -> None:
        return links_api.scan_for_links(
            self.ctx,
            text,
            parent_node,
            course_id,
            module_title,
            single,
            logger,
        )
