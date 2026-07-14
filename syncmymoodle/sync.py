import logging
from typing import cast

from syncmymoodle import filters, sync_handlers
from syncmymoodle import moodle as moodle_api
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import redact_url_secrets
from syncmymoodle.node import Node

logger = logging.getLogger(__name__)


def sync(ctx: SyncContext) -> None:  # noqa: C901 - legacy sync awaiting decomposition
    """Retrieve the file tree for all courses into ``ctx.root_node``."""
    config = ctx.config
    session = ctx.require_session()
    account = ctx.require_moodle_account()
    wstoken = account.wstoken
    user_id = account.user_id
    root_node = Node("", -1, "Root", None)
    ctx.root_node = root_node

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

    # Syncing all courses that passed the local course filters.
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

        print(f"Syncing {course_name}...")
        course_sections = moodle_api.get_course(session, wstoken, course_id)
        if course_sections is None:
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

        module_names = {
            module.get("modname")
            for section in course_sections
            if isinstance(section, dict)
            for module in section.get("modules", [])
        }

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

        for section in course_sections:
            if isinstance(section, str):
                logger.error("Moodle returned an invalid section for %s", course_name)
                continue
            if filters.should_skip_section(ctx, section, course_id):
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
                log=logger,
            )
            for module in section["modules"]:
                try:
                    if filters.should_skip_module(ctx, module, course_id):
                        continue

                    sync_handlers.handle_module(module_context, module)

                except Exception:
                    logger.exception(
                        "Failed to process Moodle module %s (%s)",
                        module.get("id"),
                        module.get("modname"),
                    )

    root_node.remove_children_nameclashes()
