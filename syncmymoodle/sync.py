import logging
import time
from dataclasses import dataclass
from typing import Any, cast

from syncmymoodle import course_cache, filters, pathing, sync_handlers
from syncmymoodle import moodle as moodle_api
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import redact_url_secrets
from syncmymoodle.node import Node

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


@dataclass
class _CourseRun:
    ctx: SyncContext
    course: _PreparedCourse
    section_total: int
    module_total: int
    assignments_by_cmid: dict[Any, Any]
    folders_by_coursemodule: dict[Any, Any]
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


def _has_complete_module_inventory(course_sections: list[object]) -> bool:
    for section in course_sections:
        if not isinstance(section, dict):
            return False
        modules = section.get("modules")
        if not isinstance(modules, list):
            return False
        for module in modules:
            if not isinstance(module, dict):
                return False
            module_id = module.get("id")
            module_kind = module.get("modname")
            if (
                not isinstance(module_id, int)
                or isinstance(module_id, bool)
                or module_id <= 0
                or not isinstance(module_kind, str)
                or not module_kind
            ):
                return False
    return True


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


def _locally_selected_courses(ctx: SyncContext) -> list[_CourseSpec]:
    account = ctx.require_moodle_account()
    courses: list[_CourseSpec] = []
    for course in moodle_api.get_all_courses(
        ctx.require_session(), account.wstoken, account.user_id
    ):
        course_id = int(course["id"])
        spec = _CourseSpec(
            filters.format_course_name(
                course.get("shortname") or f"course-{course_id}",
                ctx.config,
                logger,
            ),
            course_id,
            (course.get("idnumber") or "")[:4] or "unknown-semester",
        )
        if _course_passes_local_filters(ctx, spec):
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
            semester_node = cast(
                Node, root_node.add_child(course.semester, None, "Semester")
            )
            semester_nodes[course.semester] = semester_node
        course_node = cast(
            Node,
            semester_node.add_child(course.name, course.course_id, "Course"),
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


def _course_modules(course_sections: list[Any]) -> list[dict[str, Any]]:
    return [
        module
        for section in course_sections
        if isinstance(section, dict)
        for module in section.get("modules", [])
        if isinstance(module, dict)
    ]


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
) -> dict[Any, Any]:
    assignments = None
    if ctx.config.module_assignment and "assign" in module_names:
        account = ctx.require_moodle_account()
        assignments = moodle_api.get_assignment(
            ctx.require_session(), account.wstoken, course.course_id
        )
        if assignments is None:
            ctx.mark_course_incomplete(course.node.id)
    return {
        assignment["cmid"]: assignment
        for assignment in ((assignments or {}).get("assignments") or [])
        if "cmid" in assignment
    }


def _folders_by_coursemodule(
    ctx: SyncContext,
    course: _PreparedCourse,
    module_names: set[Any],
) -> dict[Any, Any]:
    folders: list[dict[str, Any]] | None = []
    if ctx.config.module_folder and "folder" in module_names:
        account = ctx.require_moodle_account()
        folders = moodle_api.get_folders_by_courses(
            ctx.require_session(), account.wstoken, course.course_id
        )
        if folders is None:
            ctx.mark_course_incomplete(course.node.id)
    return {folder.get("coursemodule"): folder for folder in folders or []}


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
    failed_before = run.ctx.stats.failed
    try:
        if not filters.should_skip_module(run.ctx, module, module_context.course_id):
            sync_handlers.handle_module(module_context, module)
    except Exception:
        run.ctx.stats.failed += 1
        logger.exception(
            "Failed to process Moodle module %s (%s)",
            module.get("id"),
            module.get("modname"),
        )
    if run.ctx.stats.failed > failed_before:
        run.ctx.mark_course_incomplete(run.course.node.id)
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
    section: Any,
    section_index: int,
) -> None:
    if not isinstance(section, dict):
        logger.error("Moodle returned an invalid section for %s", run.course.name)
        run.ctx.stats.failed += 1
        run.ctx.mark_course_incomplete(run.course.node.id)
        run.update_progress(section_index)
        return

    run.update_progress(section_index)
    if filters.should_skip_section(run.ctx, section, run.course.course_id):
        run.module_index += len(section["modules"])
        run.update_progress(section_index)
        return

    section_node = cast(
        Node,
        run.course.node.add_child(section["name"], section["id"], "Section"),
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
        ctx.stats.failed += 1
        _remove_course_node(root_node, course.node)
        ctx.output.sync_progress.finish_course(course_index)
        return

    section_total = len(course_sections)
    module_total = sum(
        len(section.get("modules", []))
        for section in course_sections
        if isinstance(section, dict)
    )
    run = _CourseRun(ctx, course, section_total, module_total, {}, {}, None)
    run.update_progress(0)
    modules = _course_modules(course_sections)
    if _has_complete_module_inventory(course_sections):
        course_cache.retain_current_modules(ctx, course.node, modules, logger)
    else:
        ctx.mark_course_incomplete(course.node.id)
    module_names = {module.get("modname") for module in modules}
    run.course_updates = _course_updates(ctx, course, modules)
    run.assignments_by_cmid = _assignments_by_cmid(ctx, course, module_names)
    run.folders_by_coursemodule = _folders_by_coursemodule(ctx, course, module_names)
    for section_index, section in enumerate(course_sections, start=1):
        _sync_section(run, section, section_index)
    ctx.output.sync_progress.finish_course(course_index)


def sync(ctx: SyncContext) -> None:
    """Retrieve the file tree for all courses into ``ctx.root_node``."""
    root_node = Node("", -1, "Root", None)
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
        _sync_course(ctx, root_node, course, course_index)
    pathing.resolve_node_path_clashes(root_node)
