import logging
import time
from typing import cast

from syncmymoodle import course_cache, filters, sync_handlers
from syncmymoodle import moodle as moodle_api
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import redact_url_secrets
from syncmymoodle.node import Node

logger = logging.getLogger(__name__)
SLOW_MODULE_SECONDS = 1.0


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


def sync(ctx: SyncContext) -> None:  # noqa: C901 - legacy sync awaiting decomposition
    """Retrieve the file tree for all courses into ``ctx.root_node``."""
    config = ctx.config
    session = ctx.require_session()
    account = ctx.require_moodle_account()
    wstoken = account.wstoken
    user_id = account.user_id
    root_node = Node("", -1, "Root", None)
    ctx.root_node = root_node

    ctx.output.sync_progress.discovering_courses()
    selected_courses = config.selected_courses
    course_candidates = []
    for course in moodle_api.get_all_courses(session, wstoken, user_id):
        course_name = filters.format_course_name(
            course.get("shortname") or f"course-{course.get('id')}",
            config,
            logger,
        )
        course_id = course["id"]

        if selected_courses:
            # selected_courses is an explicit allowlist that overrides
            # skip_courses, only_sync_semester and exclude_course_roles.
            if (
                filters.matching_course_filter_entry(course_id, selected_courses)
                is None
            ):
                ctx.record_filtered(
                    "courses.selected",
                    "course",
                    f"{course_name} ({course_id})",
                    "not in the configured selection",
                )
                continue
        else:
            skip_entry = filters.matching_course_filter_entry(
                course_id, config.skip_courses
            )
            if skip_entry is not None:
                ctx.record_filtered(
                    "courses.skip",
                    "course",
                    f"{course_name} ({course_id})",
                    f"matches {redact_url_secrets(skip_entry)!r}",
                )
                continue

        semestername = (course.get("idnumber") or "")[:4] or "unknown-semester"
        # Skip not selected semesters (selected_courses overrides this)
        if (
            not selected_courses
            and config.only_sync_semester
            and semestername not in config.only_sync_semester
        ):
            ctx.record_filtered(
                "courses.semesters",
                "course",
                f"{course_name} ({course_id})",
                f"semester {semestername!r} is not selected",
            )
            continue

        course_candidates.append((course_name, course_id, semestername))

    direct_roles_by_course = {}
    if not selected_courses and config.exclude_course_roles:
        direct_roles_by_course = moodle_api.get_direct_course_roles_by_course(
            session,
            wstoken,
            user_id,
            [course_id for _, course_id, _ in course_candidates],
            logger,
        )

    courses_to_sync = []
    for course_name, course_id, semestername in course_candidates:
        if config.exclude_course_roles:
            excluded_role = config.matching_excluded_course_role(
                direct_roles_by_course.get(str(course_id))
            )
            if excluded_role is not None:
                ctx.record_filtered(
                    "courses.exclude_roles",
                    "course",
                    f"{course_name} ({course_id})",
                    f"your directly assigned Moodle course role is {excluded_role!r}",
                )
                continue
        courses_to_sync.append((course_name, course_id, semestername))

    semester_nodes: dict[str, Node] = {}
    prepared_courses: list[tuple[str, int, Node]] = []
    for course_name, course_id, semestername in courses_to_sync:
        semester_node = semester_nodes.get(semestername)
        if semester_node is None:
            semester_node = cast(
                Node, root_node.add_child(semestername, None, "Semester")
            )
            semester_nodes[semestername] = semester_node
        course_node = cast(
            Node, semester_node.add_child(course_name, course_id, "Course")
        )
        prepared_courses.append((course_name, int(course_id), course_node))

    # Course paths are cache keys on disk. Resolve all course collisions before
    # any cache is read or updated so their paths cannot change mid-run.
    root_node.remove_children_nameclashes()

    progress = ctx.output.sync_progress
    progress.begin_courses(len(prepared_courses))
    # Syncing all courses that passed the local course filters.
    for course_index, (course_name, course_id, course_node) in enumerate(
        prepared_courses, start=1
    ):
        ctx.stats.courses += 1
        progress.start_course(course_index, course_name)
        course_sections = moodle_api.get_course(session, wstoken, course_id)
        if course_sections is None:
            ctx.stats.failed += 1
            semester_node = course_node.parent
            if semester_node is not None:
                semester_node.children.remove(course_node)
                if not semester_node.children:
                    root_node.children.remove(semester_node)
            progress.finish_course(course_index)
            continue

        section_total = len(course_sections)
        module_total = sum(
            len(section.get("modules", []))
            for section in course_sections
            if isinstance(section, dict)
        )
        module_index = 0
        progress.update_course(
            course_name,
            section=0,
            sections=section_total,
            module=0,
            modules=module_total,
        )

        course_modules = [
            module
            for section in course_sections
            if isinstance(section, dict)
            for module in section.get("modules", [])
            if isinstance(module, dict)
        ]
        if _has_complete_module_inventory(course_sections):
            course_cache.retain_current_modules(
                ctx, course_node, course_modules, logger
            )
        module_names = {module.get("modname") for module in course_modules}

        cached_module_updates: dict[int, int] = {}
        for module in course_modules:
            module_id = module.get("id")
            if not isinstance(module_id, int) or isinstance(module_id, bool):
                continue
            if config.module_assignment and module.get("modname") == "assign":
                assignment_entry = course_cache.get_assignment_cache_entry(
                    ctx, course_node, module_id, logger
                )
                if assignment_entry is not None:
                    cached_module_updates[module_id] = assignment_entry.since
            elif config.quiz_mode != "off" and module.get("modname") == "quiz":
                quiz_entry = course_cache.get_quiz_cache_entry(
                    ctx, course_node, module_id, logger
                )
                if quiz_entry is not None:
                    cached_module_updates[module_id] = quiz_entry.since

        course_updates = None
        if (
            cached_module_updates
            and moodle_api.MOODLE_UPDATE_FUNCTION in ctx.moodle_functions
        ):
            progress.module_status("checking for Moodle updates")
            course_updates = moodle_api.check_course_updates(
                session,
                wstoken,
                int(course_id),
                cached_module_updates,
                logger,
            )
            if course_updates is None:
                logger.info(
                    "Moodle incremental update check failed for %s; using "
                    "full module queries for this course",
                    course_name,
                )

        assignments = None
        if config.module_assignment and ("assign" in module_names):
            assignments = moodle_api.get_assignment(session, wstoken, course_id)
        assignments_by_cmid = {
            assignment["cmid"]: assignment
            for assignment in ((assignments or {}).get("assignments") or [])
            if "cmid" in assignment
        }

        folders = []
        if config.module_folder and ("folder" in module_names):
            folders = moodle_api.get_folders_by_courses(session, wstoken, course_id)
        folders_by_coursemodule = {
            folder.get("coursemodule"): folder for folder in folders
        }

        for section_index, section in enumerate(course_sections, start=1):
            if isinstance(section, str):
                logger.error("Moodle returned an invalid section for %s", course_name)
                ctx.stats.failed += 1
                progress.update_course(
                    course_name,
                    section=section_index,
                    sections=section_total,
                    module=module_index,
                    modules=module_total,
                )
                continue
            progress.update_course(
                course_name,
                section=section_index,
                sections=section_total,
                module=module_index,
                modules=module_total,
            )
            if filters.should_skip_section(ctx, section, course_id):
                module_index += len(section["modules"])
                progress.update_course(
                    course_name,
                    section=section_index,
                    sections=section_total,
                    module=module_index,
                    modules=module_total,
                )
                continue
            section_node = cast(
                Node,
                course_node.add_child(section["name"], section["id"], "Section"),
            )
            module_context = sync_handlers.ModuleContext(
                ctx=ctx,
                course_id=course_id,
                course_node=course_node,
                section_node=section_node,
                assignments_by_cmid=assignments_by_cmid,
                folders_by_coursemodule=folders_by_coursemodule,
                course_updates=course_updates,
                log=logger,
            )
            for module in section["modules"]:
                module_name = str(
                    module.get("name") or f"module {module.get('id', 'unknown')}"
                )
                module_kind = str(module.get("modname") or "unknown")
                progress.update_course(
                    course_name,
                    section=section_index,
                    sections=section_total,
                    module=module_index,
                    modules=module_total,
                    current_module=f"{module_name} [{module_kind}]",
                )
                module_started_at = time.monotonic()
                try:
                    if filters.should_skip_module(ctx, module, course_id):
                        continue

                    sync_handlers.handle_module(module_context, module)

                except Exception:
                    ctx.stats.failed += 1
                    logger.exception(
                        "Failed to process Moodle module %s (%s)",
                        module.get("id"),
                        module.get("modname"),
                    )
                finally:
                    elapsed = time.monotonic() - module_started_at
                    if elapsed >= SLOW_MODULE_SECONDS:
                        logger.info(
                            "Processed Moodle module %s (%s) %r in %.1fs",
                            module.get("id"),
                            module_kind,
                            module_name,
                            elapsed,
                        )
                    module_index += 1
                    progress.update_course(
                        course_name,
                        section=section_index,
                        sections=section_total,
                        module=module_index,
                        modules=module_total,
                    )
            if not section["modules"]:
                progress.update_course(
                    course_name,
                    section=section_index,
                    sections=section_total,
                    module=module_index,
                    modules=module_total,
                )

        progress.finish_course(course_index)

    root_node.remove_children_nameclashes()
