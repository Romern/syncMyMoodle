import base64
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, cast

import requests

from syncmymoodle import filters
from syncmymoodle.constants import (
    HTTP_TIMEOUT_SECONDS,
    RWTH_SCIEBO_STATUS_URL,
    SCIEBO_LINK_RE,
    SCIEBO_URL,
)
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import (
    HttpFailureKind,
    RequestPolicyError,
    classify_http_failure,
    get_input_value,
    parse_html,
    record_service_failure,
    request_following_safe_redirects,
    safe_request_error,
    same_origin,
)
from syncmymoodle.node import (
    DownloadKind,
    Node,
    NodeKind,
    RemoteMarkerKind,
    match_equivalent_child,
)

logger = logging.getLogger(__name__)

WEBDAV_LOCATION = "/public.php/webdav/"
_DIRECT_WEBDAV_UNSUPPORTED = object()
DAV_NAMESPACE = "{DAV:}"
OWNCLOUD_NAMESPACE = "{http://owncloud.org/ns}"


def sharing_token_from_link(link: str) -> str:
    """Return the public-share token from a ``.../s/<token>`` Sciebo URL.

    Nextcloud public shares use this final path segment as the WebDAV
    username, so it is a reliable fallback when the share page no longer
    exposes the token as a hidden ``<input name="sharingToken">``.
    """
    return link.split("?", 1)[0].rstrip("/").rsplit("/s/", 1)[-1]


PROPFIND_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <d:getetag/>
    <d:getcontentlength/>
    <oc:checksums/>
  </d:prop>
</d:propfind>"""


def _sciebo_url_allowed(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.username is None
        and parsed.password is None
        and same_origin(url, SCIEBO_URL)
    )


def _canonical_webdav_href(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    path = parsed.path
    if not path.startswith("/"):
        return None
    is_folder = path.endswith("/")
    raw_parts = path.split("/")
    if raw_parts[0] or any(not part for part in raw_parts[1:-1]):
        return None

    encoded_parts: list[str] = []
    for raw_part in raw_parts[1:-1] if is_folder else raw_parts[1:]:
        try:
            decoded_part = urllib.parse.unquote_to_bytes(raw_part).decode("utf-8")
        except UnicodeDecodeError:
            return None
        if (
            decoded_part in {"", ".", ".."}
            or "/" in decoded_part
            or "\0" in decoded_part
        ):
            return None
        encoded_parts.append(urllib.parse.quote(decoded_part, safe=""))

    normalized = "/" + "/".join(encoded_parts)
    if is_folder:
        normalized += "/"
    return normalized if normalized.startswith(WEBDAV_LOCATION) else None


def _webdav_child_href(
    parent_href: str,
    name: str,
    *,
    is_folder: bool,
) -> str | None:
    encoded_name = urllib.parse.quote(name, safe="")
    href = parent_href + encoded_name + ("/" if is_folder else "")
    return href if _canonical_webdav_href(href) == href else None


def _webdav_display_name(href: str) -> str:
    return urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1])


def _record_failure(
    ctx: SyncContext,
    kind: HttpFailureKind,
    reason: str,
    log: logging.Logger,
) -> None:
    record_service_failure(
        ctx.service_outages,
        SCIEBO_URL,
        "Sciebo",
        kind,
        reason,
        log,
        f"Check the RWTH ITC status page: {RWTH_SCIEBO_STATUS_URL}",
    )


def scan_public_shares(
    ctx: SyncContext,
    text: str,
    parent_node: Node,
    log: logging.Logger = logger,
    *,
    course_id: Any = None,
) -> None:
    for link in sorted(set(SCIEBO_LINK_RE.findall(text))):
        log.info(f"Found Sciebo Link: {link}")
        if filters.should_skip_url(
            ctx,
            link,
            "Sciebo link",
            course_id=course_id,
        ):
            continue
        if link in ctx.sciebo_link_cache:
            cached_root = ctx.sciebo_link_cache[link]
            if cached_root is None:
                _record_share_failure(ctx, parent_node, course_id, link)
            elif match_equivalent_child(parent_node, cached_root) is None:
                parent_node.children.append(cached_root.clone(parent_node))
            continue
        if ctx.service_outages.should_skip(SCIEBO_URL):
            _record_share_failure(ctx, parent_node, course_id, link)
            return
        ctx.output.sync_progress.module_status("connecting to Sciebo share")
        if not _scan_new_share(ctx, link, parent_node, log):
            _record_share_failure(ctx, parent_node, course_id, link)


def _record_share_failure(
    ctx: SyncContext,
    parent_node: Node,
    course_id: Any,
    link: str,
) -> None:
    course_node = parent_node.ancestor(NodeKind.COURSE)
    affected_course = course_node.id if course_node is not None else course_id
    ctx.record_course_failure_once(affected_course, f"sciebo:{link}")


def _cached_node_for(ctx: SyncContext, node: Node) -> Node | None:
    """Find ``node`` in an already loaded course cache without a dependency cycle."""
    course_node = node.ancestor(NodeKind.COURSE)
    if course_node is None:
        return None
    state = ctx.course_cache_states.get(course_node)
    if state is None or state.course_root is None:
        return None

    relative_nodes: list[Node] = []
    current = node
    while current is not course_node:
        relative_nodes.append(current)
        if current.parent is None:
            return None
        current = current.parent

    cached: Node | None = state.course_root
    for relative_node in reversed(relative_nodes):
        cached = match_equivalent_child(cached, relative_node)
        if cached is None:
            return None
    return cached


def _valid_cached_marker(node: Node) -> bool:
    return (node.etag is None and node.etag_kind is None) or (
        isinstance(node.etag, str) and bool(node.etag) and node.etag_kind is not None
    )


def _cached_sciebo_url(url: str | None, expected_href: str) -> str | None:
    if not url or not _sciebo_url_allowed(url):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.query or parsed.fragment:
        return None
    return (
        SCIEBO_URL + expected_href
        if _canonical_webdav_href(parsed.path) == expected_href
        else None
    )


def _restored_sciebo_children(
    cached_parent: Node,
    parent: Node,
    parent_href: str,
    auth_headers: dict[str, str],
) -> list[Node] | None:
    """Rebuild a validated cached subtree as pending nodes with current auth."""
    restored: list[Node] = []
    seen_hrefs: set[str] = set()
    for cached in cached_parent.children:
        if (
            not cached.name
            or cached.name in {".", ".."}
            or "/" in cached.name
            or not _valid_cached_marker(cached)
            or cached.download_kind is not DownloadKind.DIRECT
        ):
            return None

        is_folder = cached.type == "Sciebo Folder"
        expected_href = _webdav_child_href(
            parent_href,
            cached.name,
            is_folder=is_folder,
        )
        if expected_href is None or expected_href in seen_hrefs:
            return None
        seen_hrefs.add(expected_href)

        if is_folder:
            if cached.url is not None:
                return None
            restored_node = Node(
                cached.name,
                cached.id,
                cached.type,
                parent,
                etag=cached.etag,
                etag_kind=cached.etag_kind,
                remote_size=cached.remote_size,
                name_clash_id=cached.name_clash_id,
            )
            children = _restored_sciebo_children(
                cached,
                restored_node,
                expected_href,
                auth_headers,
            )
            if children is None:
                return None
            restored_node.children = children
        elif cached.type == "Sciebo File":
            if cached.children:
                return None
            url = _cached_sciebo_url(cached.url, expected_href)
            if url is None:
                return None
            restored_node = Node(
                cached.name,
                cached.id,
                cached.type,
                parent,
                url=url,
                download_headers=auth_headers,
                etag=cached.etag,
                etag_kind=cached.etag_kind,
                remote_size=cached.remote_size,
                name_clash_id=cached.name_clash_id,
            )
        else:
            return None
        restored.append(restored_node)
    return restored


def _restore_unchanged_sciebo_folder(
    folder: Node,
    cached_folder: Node | None,
    href: str,
    auth_headers: dict[str, str],
) -> bool:
    if (
        cached_folder is None
        or folder.etag is None
        or folder.etag_kind is None
        or folder.etag != cached_folder.etag
        or folder.etag_kind is not cached_folder.etag_kind
    ):
        return False
    children = _restored_sciebo_children(
        cached_folder,
        folder,
        href,
        auth_headers,
    )
    if children is None:
        return False
    folder.children = children
    return True


def _share_authorization(sharing_token: str) -> str:
    secret = base64.b64encode(f"{sharing_token}:null".encode()).decode()
    return f"Basic {secret}"


def _share_auth_headers(
    ctx: SyncContext,
    link: str,
    log: logging.Logger,
) -> tuple[str, dict[str, str]] | None:
    try:
        response = ctx.require_session().get(
            link,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        _record_failure(
            ctx,
            HttpFailureKind.TRANSIENT,
            f"share page request failed: {safe_request_error(error)}",
            log,
        )
        return None

    failure_kind = classify_http_failure(response.status_code)
    if failure_kind is not None:
        _record_failure(
            ctx,
            failure_kind,
            f"share page returned HTTP {response.status_code}",
            log,
        )
        if failure_kind is HttpFailureKind.RESOURCE:
            log.warning(
                "Sciebo share page returned HTTP %s; skipping this share",
                response.status_code,
            )
        return None

    soup = parse_html(response.text)
    request_token = cast(
        str | None,
        soup.head.get("data-requesttoken") if soup.head is not None else None,
    )
    if not request_token:
        _record_failure(
            ctx,
            HttpFailureKind.TRANSIENT,
            "share page returned an unexpected response without a request token",
            log,
        )
        return None

    # Newer Sciebo/Nextcloud share pages no longer render the token as a
    # hidden input. It matches the /s/<token> segment of the share URL,
    # which is what the public WebDAV endpoint expects.
    sharing_token = get_input_value(soup, "sharingToken") or sharing_token_from_link(
        link
    )
    if not sharing_token:
        ctx.service_outages.record_available(SCIEBO_URL)
        log.warning("Sciebo link did not contain a share token; skipping this share")
        return None

    return sharing_token, {
        "Authorization": _share_authorization(sharing_token),
        "requesttoken": request_token,
    }


def _scan_new_share(
    ctx: SyncContext,
    link: str,
    parent_node: Node,
    log: logging.Logger,
) -> bool:
    sharing_token = sharing_token_from_link(link)
    capability = ctx.sciebo_direct_webdav_supported
    use_legacy = capability is False
    auth_headers: dict[str, str] = {}
    root_listing = None
    if not use_legacy:
        auth_headers = {
            "Authorization": _share_authorization(sharing_token),
            "X-Requested-With": "XMLHttpRequest",
        }
        root_listing = _fetch_webdav_listing(
            ctx,
            WEBDAV_LOCATION,
            auth_headers,
            log,
            allow_legacy_fallback=True,
        )
        use_legacy = root_listing is _DIRECT_WEBDAV_UNSUPPORTED

    if use_legacy:
        share_auth = _share_auth_headers(ctx, link, log)
        if share_auth is None:
            ctx.sciebo_link_cache[link] = None
            return False
        sharing_token, auth_headers = share_auth
        root_listing = None
    elif root_listing is None:
        ctx.sciebo_link_cache[link] = None
        return False

    sciebo_root = parent_node.add_child(
        f"sciebo-{sharing_token}", None, "Sciebo Folder"
    )
    cached_root = _cached_node_for(ctx, sciebo_root)

    if _add_sciebo_files(
        ctx,
        WEBDAV_LOCATION,
        sciebo_root,
        auth_headers,
        log,
        listing=root_listing,
        cached_parent=cached_root,
    ):
        ctx.service_outages.record_available(SCIEBO_URL)
        if capability is None:
            ctx.sciebo_direct_webdav_supported = not use_legacy
        ctx.sciebo_link_cache[link] = sciebo_root.clone()
        return True

    parent_node.children.remove(sciebo_root)
    ctx.sciebo_link_cache[link] = None
    return False


def _fetch_webdav_listing(
    ctx: SyncContext,
    href: str,
    auth_header: dict[str, str],
    log: logging.Logger,
    *,
    allow_legacy_fallback: bool = False,
) -> Any | None:
    # request the URL with the PROPFIND method and a body that also asks
    # Sciebo/Nextcloud to include content checksums (oc:checksums) for each
    # item. These checksums are stable content hashes (e.g. SHA1) and allow us
    # to safely compare local files against the current remote content without
    # relying on ETags.
    headers = {
        **auth_header,
        "Depth": "1",
        "Content-Type": "application/xml",
    }
    try:
        propfind_response = request_following_safe_redirects(
            ctx.require_session(),
            "PROPFIND",
            SCIEBO_URL + href,
            _sciebo_url_allowed,
            headers=headers,
            data=PROPFIND_BODY,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except RequestPolicyError as error:
        ctx.service_outages.record_available(SCIEBO_URL)
        log.warning(
            "Sciebo WebDAV request refused: %s",
            safe_request_error(error),
        )
        return None
    except requests.RequestException as error:
        _record_failure(
            ctx,
            HttpFailureKind.TRANSIENT,
            f"WebDAV request failed: {safe_request_error(error)}",
            log,
        )
        return None

    failure_kind = classify_http_failure(propfind_response.status_code)
    if failure_kind is not None:
        if allow_legacy_fallback and failure_kind is HttpFailureKind.RESOURCE:
            propfind_response.close()
            return _DIRECT_WEBDAV_UNSUPPORTED
        _record_failure(
            ctx,
            failure_kind,
            f"WebDAV returned HTTP {propfind_response.status_code}",
            log,
        )
        if failure_kind is HttpFailureKind.RESOURCE:
            log.warning(
                "Sciebo WebDAV returned HTTP %s; skipping this share",
                propfind_response.status_code,
            )
        return None

    # Reject both maintenance HTML and truncated XML. A recovering parser could
    # turn a partial directory response into a cacheable, incomplete inventory.
    try:
        listing = ET.fromstring(propfind_response.text)
    except ET.ParseError:
        listing = None
    if (
        listing is None
        or listing.tag != DAV_NAMESPACE + "multistatus"
        or not listing.findall(DAV_NAMESPACE + "response")
    ):
        if allow_legacy_fallback:
            propfind_response.close()
            return _DIRECT_WEBDAV_UNSUPPORTED
        _record_failure(
            ctx,
            HttpFailureKind.TRANSIENT,
            "WebDAV returned an unexpected response instead of a DAV listing",
            log,
        )
        return None
    return listing


def _add_sciebo_files(
    ctx: SyncContext,
    href: str,
    parent_node: Node,
    auth_header: dict[str, str],
    log: logging.Logger = logger,
    *,
    listing: Any | None = None,
    cached_parent: Node | None = None,
) -> bool:
    ctx.output.sync_progress.module_status(f"scanning Sciebo folder {parent_node.name}")
    listing_result = (
        listing
        if listing is not None
        else _fetch_webdav_listing(ctx, href, auth_header, log)
    )
    if not isinstance(listing_result, ET.Element):
        return False

    current_href = _canonical_webdav_href(href)
    responses: list[tuple[ET.Element, str]] = []
    seen_hrefs: set[str] = set()
    for resp in listing_result.findall(DAV_NAMESPACE + "response"):
        href_tag = resp.find(DAV_NAMESPACE + "href")
        new_href = _canonical_webdav_href(
            href_tag.text if href_tag is not None else None
        )
        relative = (
            new_href[len(current_href) :].rstrip("/")
            if current_href is not None
            and new_href is not None
            and new_href.startswith(current_href)
            else None
        )
        if (
            current_href is None
            or new_href is None
            or new_href in seen_hrefs
            or (new_href != current_href and (not relative or "/" in relative))
        ):
            ctx.service_outages.record_available(SCIEBO_URL)
            log.warning("Sciebo WebDAV returned a malformed href; skipping this share")
            return False
        seen_hrefs.add(new_href)
        responses.append((resp, new_href))

    for resp, new_href in responses:
        if new_href == current_href:
            log.info(
                "Sciebo: skipping %s because it is the current folder",
                new_href,
            )
            continue

        remote_marker, remote_size = _extract_remote_metadata(resp)
        etag_value = remote_marker[0] if remote_marker else None
        etag_kind = remote_marker[1] if remote_marker else None

        log.info(f"Sciebo response href: {new_href}")
        displayname = _webdav_display_name(new_href)

        # check if the response is a folder
        if new_href.endswith("/"):
            # create a new node for the folder
            folder_node = parent_node.add_child(
                displayname,
                None,
                "Sciebo Folder",
                etag=etag_value,
                etag_kind=etag_kind,
                remote_size=remote_size,
            )
            cached_folder = match_equivalent_child(cached_parent, folder_node)
            if _restore_unchanged_sciebo_folder(
                folder_node,
                cached_folder,
                new_href,
                auth_header,
            ):
                continue
            # recursive call to get all files in the folder
            if not _add_sciebo_files(
                ctx,
                new_href,
                folder_node,
                auth_header,
                log,
                cached_parent=cached_folder,
            ):
                return False
        else:
            # create a new node for the file
            parent_node.add_download_child(
                displayname,
                None,
                "Sciebo File",
                url=SCIEBO_URL + new_href,
                download_headers=auth_header,
                etag=etag_value,
                etag_kind=etag_kind,
                remote_size=remote_size,
            )

    return True


def _successful_dav_properties(response: ET.Element) -> list[ET.Element]:
    properties: list[ET.Element] = []
    for propstat in response.findall(DAV_NAMESPACE + "propstat"):
        status = propstat.findtext(DAV_NAMESPACE + "status", default="")
        fields = status.split(maxsplit=2)
        if len(fields) < 2 or not fields[1].isdigit():
            continue
        if not 200 <= int(fields[1]) < 300:
            continue
        prop = propstat.find(DAV_NAMESPACE + "prop")
        if prop is not None:
            properties.append(prop)
    return properties


def _extract_remote_metadata(
    response_tag: ET.Element,
) -> tuple[tuple[str, RemoteMarkerKind] | None, int | None]:
    properties = _successful_dav_properties(response_tag)
    return _extract_remote_marker(properties), _extract_remote_size(properties)


def _extract_remote_marker(
    properties: list[ET.Element],
) -> tuple[str, RemoteMarkerKind] | None:
    # Prefer a stable content hash from oc:checksums; fall back to the raw ETag
    # as an opaque remote-version marker.
    for prop in properties:
        checksums_tag = prop.find(OWNCLOUD_NAMESPACE + "checksums")
        if checksums_tag is not None:
            for checksum in checksums_tag.findall(OWNCLOUD_NAMESPACE + "checksum"):
                text = (checksum.text or "").strip()
                if text.upper().startswith("SHA1:"):
                    return text.split(":", 1)[1], RemoteMarkerKind.CONTENT_HASH

    for prop in properties:
        etag_tag = prop.find(DAV_NAMESPACE + "getetag")
        if etag_tag is not None and etag_tag.text:
            return etag_tag.text.strip(), RemoteMarkerKind.OPAQUE

    return None


def _extract_remote_size(properties: list[ET.Element]) -> int | None:
    for prop in properties:
        size_tag = prop.find(DAV_NAMESPACE + "getcontentlength")
        if size_tag is None or not size_tag.text:
            continue
        try:
            size = int(size_tag.text.strip())
        except ValueError:
            continue
        return size if size >= 0 else None
    return None
