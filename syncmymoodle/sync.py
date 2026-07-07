import json
import logging
from typing import cast

from syncmymoodle import filters, sync_handlers
from syncmymoodle import moodle as moodle_api
from syncmymoodle.context import SyncContext
from syncmymoodle.node import Node

logger = logging.getLogger(__name__)


def sync(ctx: SyncContext) -> None:
    """Retrieve the file tree for all courses into ``ctx.root_node``."""
    config = ctx.config
    if not ctx.session:
        raise Exception("You need to login() first.")
    if not ctx.wstoken:
        raise Exception("You need to get_moodle_wstoken() first.")
    if not ctx.user_id:
        raise Exception("You need to get_userid() first.")
    session = ctx.require_session()
    wstoken = ctx.wstoken
    user_id = ctx.user_id
    root_node = Node("", -1, "Root", None)
    ctx.root_node = root_node

    # Syncing all courses
    for course in moodle_api.get_all_courses(session, wstoken, user_id):
        course_name = filters.format_course_name(
            course.get("shortname") or f"course-{course.get('id')}",
            config,
            logger,
        )
        course_id = course["id"]

        selected_courses = config.selected_courses
        if selected_courses:
            # selected_courses is an explicit allowlist that overrides
            # skip_courses (and, below, only_sync_semester).
            if not filters.course_id_in_filter(course_id, selected_courses):
                continue
        elif filters.course_id_in_filter(course_id, config.skip_courses):
            continue

        semestername = (course.get("idnumber") or "")[:4] or "unknown-semester"
        # Skip not selected semesters (selected_courses overrides this)
        if (
            not selected_courses
            and config.only_sync_semester
            and semestername not in config.only_sync_semester
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
        course_sections = moodle_api.get_course(session, wstoken, course_id)
        module_names = {
            module.get("modname")
            for section in course_sections
            if isinstance(section, dict)
            for module in section.get("modules", [])
        }

        assignments = None
        if config.module_enabled("assignment") and ("assign" in module_names):
            assignments = moodle_api.get_assignment(session, wstoken, course_id)
        assignments_by_cmid = {
            assignment["cmid"]: assignment
            for assignment in ((assignments or {}).get("assignments") or [])
            if "cmid" in assignment
        }

        folders = []
        if config.module_enabled("folder") and ("folder" in module_names):
            folders = moodle_api.get_folders_by_courses(session, wstoken, course_id)
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
            if filters.should_skip_section(config, section, course_id, logger):
                continue
            logger.info("------SECTION-DATA------")
            logger.info(json.dumps(section))
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
                    if filters.should_skip_module(config, module, course_id, logger):
                        continue

                    sync_handlers.handle_module(module_context, module)

                except Exception:
                    logger.exception(f"Failed to download the module {module}")

    root_node.remove_children_nameclashes()
