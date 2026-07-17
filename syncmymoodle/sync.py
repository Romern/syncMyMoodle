import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from syncmymoodle import course_cache, filters, links, pathing, sync_handlers
from syncmymoodle import moodle as moodle_api
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import canonical_remote_url, redact_url_secrets
from syncmymoodle.node import DownloadKind, Node, NodeKind
from syncmymoodle.outcomes import RemovedContent
from syncmymoodle.pathing import sanitized_node_path_parts

logger = logging.getLogger(__name__)
SLOW_MODULE_SECONDS = 1.0


@dataclass(frozen=True)
class _CourseSpec:
    name: str
    course_id: int
    semester: str


@dataclass(frozen=True)
class _PreparedCourse:
    name: str
    course_id: int
    node: Node


def _course_inventory_scope(ctx: SyncContext, course_id: int) -> str:
    """Describe configured policy that controls which remote nodes enter a tree."""

    def patterns(value: dict[str, list[str]]) -> list[str]:
        return sorted(set(filters.pattern_list(value, course_id)))

    config = ctx.config
    scope = {
        "version": 1,
        "filters": {
            "allowed_domains": patterns(config.allowed_domains),
            "exclude_links": patterns(config.exclude_links),
            "exclude_modules": patterns(config.exclude_modules),
            "exclude_sections": patterns(config.exclude_sections),
        },
        "links": {
            "follow": config.follow_links,
            "youtube": config.link_youtube,
            "opencast": config.link_opencast,
            "sciebo": config.link_sciebo,
            "emedia": config.link_emedia,
        },
        "modules": {
            "assignment": config.module_assignment,
            "resource": config.module_resource,
            "folder": config.module_folder,
            "quiz": config.quiz_mode,
        },
    }
    encoded = json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _remote_content_identity(node: Node) -> tuple[str, str] | None:
    """Return a stable comparison key and a safe user-visible identity."""
    if not node.url:
        return None
    youtube_id = links.youtube_video_id_from_node(node)
    if youtube_id is not None:
        identity = f"youtube:{youtube_id}"
        return identity, identity
    identity_url, display_url = canonical_remote_url(node.url)
    if node.download_kind is DownloadKind.OPENCAST:
        identity = f"opencast:{node.id}:{identity_url.partition('?')[0]}"
        return identity, identity
    if node.download_kind in {DownloadKind.EMEDIA, DownloadKind.QUIZ} and node.id:
        identity = f"{node.download_kind}:{node.id}"
        return identity, identity
    return f"{node.download_kind}:{identity_url}", display_url


def _remote_content_nodes(root: Node) -> dict[str, list[tuple[Node, str]]]:
    nodes: dict[str, list[tuple[Node, str]]] = {}
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(node.children)
        identity = _remote_content_identity(node)
        if identity is not None:
            key, display = identity
            nodes.setdefault(key, []).append((node, display))
    return nodes


def _old_course_relative_path(node: Node) -> str:
    parts = sanitized_node_path_parts(node)[1:]
    return PurePosixPath(*parts).as_posix()


def _removed_course_content(
    course: _PreparedCourse,
    old_course_root: Node,
) -> set[RemovedContent]:
    """Find unambiguous remote identities absent from the current course tree."""
    old_nodes = _remote_content_nodes(old_course_root)
    current_identities = _remote_content_nodes(course.node)
    course_label = f"{course.name} ({course.course_id})"
    return {
        RemovedContent(course_label, _old_course_relative_path(node), display)
        for identity, candidates in old_nodes.items()
        if identity not in current_identities and len(candidates) == 1
        for node, display in candidates
    }


def _record_removed_content(
    ctx: SyncContext,
    courses: list[_PreparedCourse],
) -> None:
    for course in courses:
        if course.course_id in ctx.incomplete_course_ids:
            continue
        old_course_root = course_cache.comparable_course_cache_root(
            ctx,
            course.node,
            _course_inventory_scope(ctx, course.course_id),
            logger,
        )
        if course.course_id in ctx.inventory_filtered_course_ids:
            continue
        if old_course_root is not None:
            ctx.removed_content.update(_removed_course_content(course, old_course_root))


@dataclass
class _CourseRun:
    ctx: SyncContext
    course: _PreparedCourse
    section_total: int
    module_total: int
    assignments_by_cmid: dict[int, dict[str, Any]]
    folders_by_coursemodule: dict[int, dict[str, Any]]
    course_updates: moodle_api.CourseUpdates | None
    module_index: int = 0

    def update_progress(
        self,
        section_index: int,
        current_module: str = "",
    ) -> None:
        self.ctx.output.sync_progress.update_course(
            self.course.name,
            section=section_index,
            sections=self.section_total,
            module=self.module_index,
            modules=self.module_total,
            current_module=current_module,
        )


def _positive_int(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _normalized_course_sections(
    value: object,
) -> tuple[list[dict[str, Any]], bool]:
    """Return a safe course inventory and whether Moodle supplied it intact."""
    if not isinstance(value, list):
        return [], False

    complete = True
    sections: list[dict[str, Any]] = []
    for raw_section in value:
        if not isinstance(raw_section, dict):
            complete = False
            continue

        section = dict(raw_section)
        section_id = _positive_int(section.get("id"))
        section_name = section.get("name")
        if section_id is None or not isinstance(section_name, str) or not section_name:
            complete = False
            section["id"] = section_id
            section["name"] = (
                section_name
                if isinstance(section_name, str) and section_name
                else f"section {section_id or 'unknown'}"
            )

        raw_modules = section.get("modules")
        if not isinstance(raw_modules, list):
            complete = False
            raw_modules = []
        modules: list[dict[str, Any]] = []
        for raw_module in raw_modules:
            if not isinstance(raw_module, dict):
                complete = False
                continue
            module = dict(raw_module)
            module_id = _positive_int(module.get("id"))
            module_kind = module.get("modname")
            if module_id is None or not isinstance(module_kind, str) or not module_kind:
                complete = False
                continue
            if not isinstance(module.get("name"), str) or not module["name"]:
                complete = False
                module["name"] = f"module {module_id}"
            modules.append(module)
        section["modules"] = modules
        sections.append(section)
    return sections, complete


def _course_passes_local_filters(ctx: SyncContext, course: _CourseSpec) -> bool:
    config = ctx.config
    selected_courses = config.selected_courses
    label = f"{course.name} ({course.course_id})"
    if selected_courses:
        if (
            filters.matching_course_filter_entry(course.course_id, selected_courses)
            is not None
        ):
            return True
        ctx.record_filtered(
            "courses.selected",
            "course",
            label,
            "not in the configured selection",
        )
        return False

    skip_entry = filters.matching_course_filter_entry(
        course.course_id, config.skip_courses
    )
    if skip_entry is not None:
        ctx.record_filtered(
            "courses.skip",
            "course",
            label,
            f"matches {redact_url_secrets(skip_entry)!r}",
        )
        return False
    if config.only_sync_semester and course.semester not in config.only_sync_semester:
        ctx.record_filtered(
            "courses.semesters",
            "course",
            label,
            f"semester {course.semester!r} is not selected",
        )
        return False
    return True


def _course_spec_from_summary(
    ctx: SyncContext,
    value: object,
    position: int,
) -> _CourseSpec | None:
    if not isinstance(value, dict):
        logger.error(
            "Ignoring malformed Moodle course summary at position %s", position
        )
        ctx.stats.failed += 1
        return None
    course_id = _positive_int(value.get("id"))
    if course_id is None:
        logger.error(
            "Ignoring Moodle course summary with an invalid id at position %s",
            position,
        )
        ctx.stats.failed += 1
        return None

    shortname = value.get("shortname")
    idnumber = value.get("idnumber")
    malformed = (shortname is not None and not isinstance(shortname, str)) or (
        idnumber is not None and not isinstance(idnumber, str)
    )
    if malformed:
        logger.error(
            "Ignoring malformed Moodle course summary for course %s",
            course_id,
        )
        ctx.stats.failed += 1
        return None
    safe_shortname = shortname if isinstance(shortname, str) else ""
    safe_idnumber = idnumber if isinstance(idnumber, str) else ""
    return _CourseSpec(
        filters.format_course_name(
            safe_shortname or f"course-{course_id}",
            ctx.config,
            logger,
        ),
        course_id,
        safe_idnumber[:4] or "unknown-semester",
    )


def _locally_selected_courses(ctx: SyncContext) -> list[_CourseSpec]:
    account = ctx.require_moodle_account()
    courses: list[_CourseSpec] = []
    summaries = moodle_api.get_all_courses(
        ctx.require_session(), account.wstoken, account.user_id
    )
    if not isinstance(summaries, list):
        logger.error("Moodle returned a malformed course summary inventory")
        ctx.stats.failed += 1
        return courses
    for position, summary in enumerate(summaries, start=1):
        spec = _course_spec_from_summary(ctx, summary, position)
        if spec is not None and _course_passes_local_filters(ctx, spec):
            courses.append(spec)
    return courses


def _courses_after_role_filter(
    ctx: SyncContext,
    candidates: list[_CourseSpec],
) -> list[_CourseSpec]:
    config = ctx.config
    if config.selected_courses or not config.exclude_course_roles:
        return candidates
    account = ctx.require_moodle_account()
    direct_roles_by_course = moodle_api.get_direct_course_roles_by_course(
        ctx.require_session(),
        account.wstoken,
        account.user_id,
        [course.course_id for course in candidates],
        logger,
    )

    courses: list[_CourseSpec] = []
    for course in candidates:
        excluded_role = config.matching_excluded_course_role(
            direct_roles_by_course.get(str(course.course_id))
        )
        if excluded_role is not None:
            ctx.record_filtered(
                "courses.exclude_roles",
                "course",
                f"{course.name} ({course.course_id})",
                f"your directly assigned Moodle course role is {excluded_role!r}",
            )
            continue
        courses.append(course)
    return courses


def _prepare_course_nodes(
    root_node: Node,
    courses: list[_CourseSpec],
) -> list[_PreparedCourse]:
    semester_nodes: dict[str, Node] = {}
    prepared: list[_PreparedCourse] = []
    for course in courses:
        semester_node = semester_nodes.get(course.semester)
        if semester_node is None:
            semester_node = root_node.add_child(
                course.semester, None, NodeKind.SEMESTER
            )
            semester_nodes[course.semester] = semester_node
        course_node = semester_node.add_child(
            course.name, course.course_id, NodeKind.COURSE
        )
        prepared.append(_PreparedCourse(course.name, course.course_id, course_node))
    return prepared


def _remove_course_node(root_node: Node, course_node: Node) -> None:
    semester_node = course_node.parent
    if semester_node is None:
        return
    semester_node.children.remove(course_node)
    if not semester_node.children:
        root_node.children.remove(semester_node)


def _cached_module_update_times(
    ctx: SyncContext,
    course_node: Node,
    modules: list[dict[str, Any]],
) -> dict[int, int]:
    cached_update_times: dict[int, int] = {}
    for module in modules:
        module_id = module.get("id")
        if not isinstance(module_id, int) or isinstance(module_id, bool):
            continue
        since = None
        if ctx.config.module_assignment and module.get("modname") == "assign":
            assignment_entry = course_cache.get_assignment_cache_entry(
                ctx, course_node, module_id, logger
            )
            since = assignment_entry.since if assignment_entry is not None else None
        elif ctx.config.quiz_mode != "off" and module.get("modname") == "quiz":
            quiz_entry = course_cache.get_quiz_cache_entry(
                ctx, course_node, module_id, logger
            )
            since = quiz_entry.since if quiz_entry is not None else None
        if since is not None:
            cached_update_times[module_id] = since
    return cached_update_times


def _course_updates(
    ctx: SyncContext,
    course: _PreparedCourse,
    modules: list[dict[str, Any]],
) -> moodle_api.CourseUpdates | None:
    cached_update_times = _cached_module_update_times(ctx, course.node, modules)
    if (
        not cached_update_times
        or moodle_api.MOODLE_UPDATE_FUNCTION not in ctx.moodle_functions
    ):
        return None

    ctx.output.sync_progress.module_status("checking for Moodle updates")
    account = ctx.require_moodle_account()
    updates = moodle_api.check_course_updates(
        ctx.require_session(),
        account.wstoken,
        course.course_id,
        cached_update_times,
        logger,
    )
    if updates is None:
        logger.info(
            "Moodle incremental update check failed for %s; using full module "
            "queries for this course",
            course.name,
        )
    return updates


def _assignments_by_cmid(
    ctx: SyncContext,
    course: _PreparedCourse,
    module_names: set[Any],
) -> dict[int, dict[str, Any]]:
    assignments = None
    if ctx.config.module_assignment and "assign" in module_names:
        account = ctx.require_moodle_account()
        assignments = moodle_api.get_assignment(
            ctx.require_session(), account.wstoken, course.course_id
        )
        if assignments is None:
            ctx.record_course_failure(course.node.id)
            return {}
        indexed = _inventory_by_positive_id(assignments.get("assignments"), "cmid")
        if indexed is None or any(
            _positive_int(assignment.get("id")) is None
            for assignment in indexed.values()
        ):
            logger.error(
                "Moodle returned a malformed assignment inventory for %s", course.name
            )
            ctx.record_course_failure(course.node.id)
            return {}
        return indexed
    return {}


def _inventory_by_positive_id(
    value: object,
    key: str,
) -> dict[int, dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    indexed: dict[int, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            return None
        item_id = _positive_int(item.get(key))
        if item_id is None or item_id in indexed:
            return None
        indexed[item_id] = item
    return indexed


def _folders_by_coursemodule(
    ctx: SyncContext,
    course: _PreparedCourse,
    module_names: set[Any],
) -> dict[int, dict[str, Any]]:
    folders: list[dict[str, Any]] | None = []
    if (
        ctx.config.module_folder
        and ctx.config.follow_links
        and "folder" in module_names
    ):
        account = ctx.require_moodle_account()
        folders = moodle_api.get_folders_by_courses(
            ctx.require_session(), account.wstoken, course.course_id
        )
        if folders is None:
            ctx.record_course_failure(course.node.id)
            return {}
        indexed = _inventory_by_positive_id(folders, "coursemodule")
        if indexed is None:
            logger.error(
                "Moodle returned a malformed folder inventory for %s", course.name
            )
            ctx.record_course_failure(course.node.id)
            return {}
        return indexed
    return {}


def _sync_module(
    run: _CourseRun,
    module_context: sync_handlers.ModuleContext,
    module: dict[str, Any],
    section_index: int,
) -> None:
    module_name = str(module.get("name") or f"module {module.get('id', 'unknown')}")
    module_kind = str(module.get("modname") or "unknown")
    run.update_progress(section_index, f"{module_name} [{module_kind}]")
    module_started_at = time.monotonic()
    try:
        if not filters.should_skip_module(run.ctx, module, module_context.course_id):
            sync_handlers.handle_module(module_context, module)
    except Exception:
        module_context.fail()
        logger.exception(
            "Failed to process Moodle module %s (%s)",
            module.get("id"),
            module.get("modname"),
        )
    elapsed = time.monotonic() - module_started_at
    if elapsed >= SLOW_MODULE_SECONDS:
        logger.info(
            "Processed Moodle module %s (%s) %r in %.1fs",
            module.get("id"),
            module_kind,
            module_name,
            elapsed,
        )
    run.module_index += 1
    run.update_progress(section_index)


def _sync_section(
    run: _CourseRun,
    section: dict[str, Any],
    section_index: int,
) -> None:
    run.update_progress(section_index)
    if filters.should_skip_section(run.ctx, section, run.course.course_id):
        run.module_index += len(section["modules"])
        run.update_progress(section_index)
        return

    section_node = run.course.node.add_child(
        section["name"], section["id"], NodeKind.SECTION
    )
    module_context = sync_handlers.ModuleContext(
        ctx=run.ctx,
        course_id=run.course.course_id,
        course_node=run.course.node,
        section_node=section_node,
        assignments_by_cmid=run.assignments_by_cmid,
        folders_by_coursemodule=run.folders_by_coursemodule,
        course_updates=run.course_updates,
        log=logger,
    )
    for module in section["modules"]:
        _sync_module(run, module_context, module, section_index)
    if not section["modules"]:
        run.update_progress(section_index)


def _sync_course(
    ctx: SyncContext,
    root_node: Node,
    course: _PreparedCourse,
    course_index: int,
) -> None:
    account = ctx.require_moodle_account()
    course_sections = moodle_api.get_course(
        ctx.require_session(), account.wstoken, course.course_id
    )
    if course_sections is None:
        ctx.record_course_failure(course.node.id)
        _remove_course_node(root_node, course.node)
        ctx.output.sync_progress.finish_course(course_index)
        return

    course_sections, complete_inventory = _normalized_course_sections(course_sections)
    section_total = len(course_sections)
    module_total = sum(len(section["modules"]) for section in course_sections)
    run = _CourseRun(ctx, course, section_total, module_total, {}, {}, None)
    run.update_progress(0)
    modules = [module for section in course_sections for module in section["modules"]]
    if complete_inventory:
        course_cache.retain_current_modules(ctx, course.node, modules, logger)
    else:
        logger.error("Moodle returned a malformed inventory for %s", course.name)
        ctx.record_course_failure(course.node.id)
    module_names = {module.get("modname") for module in modules}
    run.course_updates = _course_updates(ctx, course, modules)
    run.assignments_by_cmid = _assignments_by_cmid(ctx, course, module_names)
    run.folders_by_coursemodule = _folders_by_coursemodule(ctx, course, module_names)
    for section_index, section in enumerate(course_sections, start=1):
        _sync_section(run, section, section_index)
    ctx.output.sync_progress.finish_course(course_index)


def _sync_course_safely(
    ctx: SyncContext,
    root_node: Node,
    course: _PreparedCourse,
    course_index: int,
) -> None:
    try:
        _sync_course(ctx, root_node, course, course_index)
    except Exception:
        ctx.record_course_failure(course.node.id)
        logger.exception("Failed to process Moodle course %s", course.name)
        ctx.output.sync_progress.finish_course(course_index)


def sync(ctx: SyncContext) -> None:
    """Retrieve the file tree for all courses into ``ctx.root_node``."""
    root_node = Node("", -1, NodeKind.ROOT, None)
    ctx.root_node = root_node
    ctx.output.sync_progress.discovering_courses()
    courses = _courses_after_role_filter(ctx, _locally_selected_courses(ctx))

    prepared_courses = _prepare_course_nodes(root_node, courses)
    pathing.resolve_node_path_clashes(root_node)
    progress = ctx.output.sync_progress
    progress.begin_courses(len(prepared_courses))
    for course_index, course in enumerate(prepared_courses, start=1):
        ctx.stats.courses += 1
        progress.start_course(course_index, course.name)
        _sync_course_safely(ctx, root_node, course, course_index)
    pathing.resolve_node_path_clashes(root_node)
    _record_removed_content(ctx, prepared_courses)
