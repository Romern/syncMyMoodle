import urllib.parse

from syncmymoodle.node import NAME_CLASH_ID_UNSET
from syncmymoodle.pathing import sanitize_path_part


def get_or_add_child(parent_node, name, id, type):
    for child in parent_node.children:
        if child.name == name and child.type == type:
            return child
    return parent_node.add_child(name, id, type)


def add_moodle_file_node(
    parent_node,
    invalid_chars,
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
        sanitize_path_part(segment, invalid_chars)
        for segment in str(moodle_filepath or "").strip("/").split("/")
        if segment
    ]

    for segment in path_segments:
        target_node = get_or_add_child(target_node, segment, None, "Folder")
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


def add_moodle_content_file_node(
    parent_node,
    invalid_chars,
    content,
    file_type=None,
):
    file_url = content.get("fileurl")
    if not file_url:
        return None

    mimetype = content.get("mimetype") or "unknown"
    filename = urllib.parse.urlsplit(file_url).path.split("/")[-1]
    if not filename:
        filename = content.get("filename")
    return add_moodle_file_node(
        parent_node,
        invalid_chars,
        "/",
        filename,
        file_url,
        file_type or f"Linked file [{mimetype}]",
        file_url,
        timemodified=content.get("timemodified"),
        name_clash_id=None,
    )


def is_direct_moodle_file_content(module, content):
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

    # Page modules often expose their rendered body as index.html. Keep that
    # path in the HTML scanner, but direct-add binary attachments.
    if modname == "page" and content.get("filename") != "index.html":
        return True

    return False
