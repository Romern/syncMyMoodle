from dataclasses import dataclass
from typing import Any


@dataclass
class ModuleServices:
    add_moodle_file_node: Any
    add_moodle_content_file_node: Any
    get_assignment_submission_files: Any
    is_direct_moodle_file_content: Any
    scan_for_links: Any
    should_skip_url: Any


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
