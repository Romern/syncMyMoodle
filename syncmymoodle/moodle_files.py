import urllib.parse
from typing import Any

from syncmymoodle.constants import HASH_ALGOS_BY_LENGTH, MOODLE_URL
from syncmymoodle.http_utils import (
    HTML_CONTENT_TYPES,
    filename_from_url,
    media_type_without_parameters,
    same_origin,
)
from syncmymoodle.node import NAME_CLASH_ID_UNSET, Node, RemoteMarkerKind
from syncmymoodle.pathing import sanitize_path_part


def canonicalize_moodle_file_url(url: str) -> str:
    """Normalize Moodle file endpoint quirks without changing external URLs."""
    if not same_origin(url, MOODLE_URL):
        return url

    parsed = urllib.parse.urlsplit(url)
    path = parsed.path
    webservice_prefix = "/webservice/pluginfile.php/"
    if path.startswith(webservice_prefix):
        path = f"/pluginfile.php/{path.removeprefix(webservice_prefix)}"
    legacy_page_prefix = "/mod_page/content/3/"
    if legacy_page_prefix in path:
        path = path.replace(legacy_page_prefix, "/mod_page/content/", 1)
    query = urllib.parse.urlencode(
        [
            (name, value)
            for name, value in urllib.parse.parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if name.casefold() != "forcedownload"
        ]
    )
    return urllib.parse.urlunsplit(parsed._replace(path=path, query=query))


def get_or_add_child(
    parent_node: Node,
    name: str,
    id: Any,  # noqa: A002 - keep Moodle payload name
    type: str,  # noqa: A002 - keep Moodle payload name
) -> Node:
    filesystem_name = sanitize_path_part(name).casefold()
    for child in parent_node.children:
        if (
            child.type == type
            and sanitize_path_part(child.name).casefold() == filesystem_name
        ):
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
    remote_content_hash: Any = None,
) -> Node:
    if url is not None:
        url = canonicalize_moodle_file_url(url)
    target_node = parent_node
    path_segments = [
        segment
        for segment in str(moodle_filepath or "").strip("/").split("/")
        if segment
    ]

    for segment in path_segments:
        target_node = get_or_add_child(target_node, segment, None, "Folder")

    content_hash = (
        remote_content_hash.lower()
        if isinstance(remote_content_hash, str)
        and len(remote_content_hash) in HASH_ALGOS_BY_LENGTH
        and all(
            character in "0123456789abcdefABCDEF" for character in remote_content_hash
        )
        else None
    )
    kwargs = {
        "timemodified": timemodified,
        "etag": content_hash,
        "etag_kind": RemoteMarkerKind.CONTENT_HASH if content_hash else None,
        "remote_size": remote_size,
        "name_clash_id": name_clash_id,
    }
    if url is None:
        return target_node.add_child(filename, id, type, **kwargs)
    return target_node.add_download_child(filename, id, type, url=url, **kwargs)


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
        remote_content_hash=content.get("contenthash"),
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
