import base64
import logging
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
    classify_http_failure,
    get_input_value,
    parse_html,
    parse_xml,
    record_service_failure,
    safe_request_error,
)
from syncmymoodle.node import Node, RemoteMarkerKind

logger = logging.getLogger(__name__)

WEBDAV_LOCATION = "/public.php/webdav/"


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
    <d:getlastmodified/>
    <d:getetag/>
    <d:getcontentlength/>
    <oc:checksums/>
  </d:prop>
</d:propfind>"""


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
) -> None:
    for link in sorted(set(SCIEBO_LINK_RE.findall(text))):
        if ctx.service_outages.should_skip(SCIEBO_URL):
            return
        log.info(f"Found Sciebo Link: {link}")
        if filters.should_skip_url(ctx, link, "Sciebo link"):
            continue
        if _reuse_cached_share(ctx, link, parent_node):
            continue
        ctx.output.sync_progress.module_status("connecting to Sciebo share")
        if not _scan_new_share(ctx, link, parent_node, log):
            current: Node | None = parent_node
            while current is not None and current.type != "Course":
                current = current.parent
            if current is not None:
                ctx.mark_course_incomplete(current.id)


def _reuse_cached_share(
    ctx: SyncContext,
    link: str,
    parent_node: Node,
) -> bool:
    if link not in ctx.sciebo_link_cache:
        return False
    cached_root = ctx.sciebo_link_cache[link]
    if cached_root is None:
        return True
    if not any(
        child.name == cached_root.name and child.type == cached_root.type
        for child in parent_node.children
    ):
        parent_node.children.append(cached_root.clone(parent_node))
    return True


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

    base_auth_secret = base64.b64encode(f"{sharing_token}:null".encode()).decode()
    return sharing_token, {
        "Authorization": f"Basic {base_auth_secret}",
        "requesttoken": request_token,
    }


def _scan_new_share(
    ctx: SyncContext,
    link: str,
    parent_node: Node,
    log: logging.Logger,
) -> bool:
    share_auth = _share_auth_headers(ctx, link, log)
    if share_auth is None:
        ctx.sciebo_link_cache[link] = None
        return False
    sharing_token, auth_headers = share_auth

    sciebo_root = parent_node.add_child(
        f"sciebo-{sharing_token}", None, "Sciebo Folder"
    )
    if sciebo_root is None:
        return True

    if _add_sciebo_files(
        ctx,
        WEBDAV_LOCATION,
        sciebo_root,
        sharing_token,
        auth_headers,
        log,
    ):
        ctx.service_outages.record_available(SCIEBO_URL)
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
        propfind_response = ctx.require_session().request(
            "PROPFIND",
            SCIEBO_URL + href,
            headers=headers,
            data=PROPFIND_BODY,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
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

    # A maintenance proxy can return an HTML error document with HTTP 200.
    soup_xml = parse_xml(propfind_response.text)
    if soup_xml.find("d:multistatus") is None or soup_xml.find("d:response") is None:
        _record_failure(
            ctx,
            HttpFailureKind.TRANSIENT,
            "WebDAV returned an unexpected response instead of a DAV listing",
            log,
        )
        return None
    return soup_xml


def _add_sciebo_files(
    ctx: SyncContext,
    href: str,
    parent_node: Node,
    sharing_token: str,
    auth_header: dict[str, str],
    log: logging.Logger = logger,
) -> bool:
    ctx.output.sync_progress.module_status(f"scanning Sciebo folder {parent_node.name}")
    soup_xml = _fetch_webdav_listing(ctx, href, auth_header, log)
    if soup_xml is None:
        return False

    for resp in soup_xml.find_all("d:response"):
        # get the href of the response
        href_tag = resp.find("d:href")
        if href_tag is None or not href_tag.text:
            continue
        new_href = href_tag.text

        if new_href == href:
            log.info(
                "Sciebo: skipping %s because it is the current folder",
                new_href,
            )
            continue

        remote_marker = _extract_remote_marker(resp)
        etag_value = remote_marker[0] if remote_marker else None
        etag_kind = remote_marker[1] if remote_marker else None
        remote_size = _extract_remote_size(resp)

        log.info(f"Sciebo response href: {new_href}")
        # get the displayname of the response
        displayname = (
            new_href.split("/")[-2]
            if new_href.endswith("/")
            else new_href.split("/")[-1]
        )
        displayname = (
            f"sciebo-{sharing_token}" if displayname == "webdav" else displayname
        )

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
            if folder_node is None:
                continue
            # recursive call to get all files in the folder
            if not _add_sciebo_files(
                ctx, new_href, folder_node, sharing_token, auth_header, log
            ):
                return False
        else:
            # create a new node for the file
            parent_node.add_child(
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


def _extract_remote_marker(response_tag: Any) -> tuple[str, RemoteMarkerKind] | None:
    # Prefer a stable content hash from oc:checksums; fall back to the raw ETag
    # as an opaque remote-version marker.
    prop = response_tag.find("d:prop")
    if prop is None:
        return None

    checksums_tag = prop.find("oc:checksums")
    if checksums_tag is not None:
        for cs in checksums_tag.find_all("oc:checksum"):
            text = (cs.text or "").strip()
            if text.upper().startswith("SHA1:"):
                return text.split(":", 1)[1], RemoteMarkerKind.CONTENT_HASH

    etag_tag = prop.find("d:getetag")
    if etag_tag and etag_tag.text:
        return str(etag_tag.text).strip(), RemoteMarkerKind.OPAQUE

    return None


def _extract_remote_size(response_tag: Any) -> int | None:
    prop = response_tag.find("d:prop")
    if prop is None:
        return None
    size_tag = prop.find("d:getcontentlength")
    if size_tag is None or not size_tag.text:
        return None
    try:
        size = int(size_tag.text.strip())
    except ValueError:
        return None
    return size if size >= 0 else None
