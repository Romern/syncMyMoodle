from typing import Any

from syncmymoodle.http_utils import (
    HTML_CONTENT_TYPES,
    filename_from_url,
    media_type_without_parameters,
)
from syncmymoodle.node import NAME_CLASH_ID_UNSET, Node
from syncmymoodle.pathing import sanitize_path_part


def get_or_add_child(
    parent_node: Node,
    name: str,
    id: Any,  # noqa: A002 - keep Moodle payload name
    type: str,  # noqa: A002 - keep Moodle payload name
) -> Node | None:
    for child in parent_node.children:
        if child.name == name and child.type == type:
            return child
    return parent_node.add_child(name, id, type)


def add_moodle_file_node(
    parent_node: Node,
    moodle_filepath: Any,
    filename: str,
    id: Any,  # noqa: A002 - keep Moodle payload name
    type: str,  # noqa: A002 - keep Moodle payload name
    url: str | None,
    timemodified: Any = None,
    remote_size: int | None = None,
    name_clash_id: Any = NAME_CLASH_ID_UNSET,
) -> Node | None:
    target_node: Node | None = parent_node
    path_segments = [
        sanitize_path_part(segment)
        for segment in str(moodle_filepath or "").strip("/").split("/")
        if segment
    ]

    for segment in path_segments:
        if target_node is None:
            return None
        child_node = get_or_add_child(target_node, segment, None, "Folder")
        if child_node is None:
            return None
        target_node = child_node

    if target_node is None:
        return None

    return target_node.add_child(
        filename,
        id,
        type,
        url=url,
        timemodified=timemodified,
        remote_size=remote_size,
        name_clash_id=name_clash_id,
    )


def add_moodle_content_file_node(
    parent_node: Node,
    content: dict[str, Any],
    file_type: str | None = None,
) -> Node | None:
    file_url = content.get("fileurl")
    if not file_url:
        return None

    mimetype = content.get("mimetype") or "unknown"
    # A fileurl whose path ends in "/" yields an empty segment, and the payload
    # may lack a filename too; a None node name would crash path sanitization
    # at download time, so fall back to a placeholder (name-clash resolution
    # disambiguates duplicates by URL).
    filename = filename_from_url(file_url) or content.get("filename") or "file"
    return add_moodle_file_node(
        parent_node,
        "/",
        filename,
        file_url,
        file_type or f"Linked file [{mimetype}]",
        file_url,
        timemodified=content.get("timemodified"),
        remote_size=content.get("filesize"),
        name_clash_id=None,
    )


def is_direct_moodle_file_content(
    module: dict[str, Any], content: dict[str, Any]
) -> bool:
    file_url = content.get("fileurl")
    if not file_url or content.get("type") != "file":
        return False

    mimetype = media_type_without_parameters(content.get("mimetype"))
    if not mimetype or mimetype in {"document/unknown", "unknown"} | HTML_CONTENT_TYPES:
        return False
    if mimetype.startswith("text/"):
        return False

    modname = module.get("modname")
    if modname in {"resource", "pdfannotator"}:
        return True

    # Page modules often expose their rendered body as index.html. Keep that
    # path in the HTML scanner, but direct-add binary attachments.
    if modname == "page" and content.get("filename") != "index.html":
        return True

    return False
