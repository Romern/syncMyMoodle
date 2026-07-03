import base64
import logging
from typing import Any, cast

from bs4 import BeautifulSoup as bs

from syncmymoodle import filters
from syncmymoodle.constants import SCIEBO_LINK_RE
from syncmymoodle.context import SyncContext
from syncmymoodle.node import Node

logger = logging.getLogger(__name__)

SCIEBO_URL = "https://rwth-aachen.sciebo.de"
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
    <oc:checksums/>
  </d:prop>
</d:propfind>"""


def scan_public_shares(
    ctx: SyncContext,
    text: str,
    parent_node: Node,
    log: logging.Logger = logger,
) -> None:
    for link in set(SCIEBO_LINK_RE.findall(text)):
        log.info(f"Found Sciebo Link: {link}")
        if filters.should_skip_url(ctx.config, link, "Sciebo link", log):
            continue
        cached_sciebo_root = ctx.sciebo_link_cache.get(link)
        if cached_sciebo_root is not None:
            if any(
                child.name == cached_sciebo_root.name
                and child.type == cached_sciebo_root.type
                for child in parent_node.children
            ):
                continue
            parent_node.children.append(cached_sciebo_root.clone(parent_node))
            continue

        # get the download page
        try:
            response = ctx.require_session().get(link)
        except Exception:
            log.exception(f"Failed to fetch Sciebo link {link}")
            continue

        # parse html code
        soup = bs(response.text, features="lxml")

        # get the requesttoken
        requestToken = cast(
            str | None,
            soup.head.get("data-requesttoken") if soup.head is not None else None,
        )
        if not requestToken:
            log.warning("Sciebo: missing request token for link %s, skipping", link)
            continue
        log.info(f"Sciebo request token: {requestToken}")

        # get the property value of the input tag with the name sharingToken
        sharing_input = soup.find("input", {"name": "sharingToken"})
        if sharing_input and sharing_input.get("value"):
            sharingToken = cast(str, sharing_input["value"])
        else:
            # Newer Sciebo/Nextcloud share pages no longer render the token as a
            # hidden input. It matches the /s/<token> segment of the share URL,
            # which is what the public WebDAV endpoint expects, so fall back to
            # deriving it from the link instead of skipping the share.
            sharingToken = sharing_token_from_link(link)
        if not sharingToken:
            log.warning("Sciebo: missing sharingToken for link %s, skipping", link)
            continue
        log.info(f"Sciebo sharingToken: {sharingToken}")

        # get baseauthentication secret
        baseAuthSecret = base64.b64encode(f"{sharingToken}:null".encode()).decode()
        log.info("Sciebo base auth secret derived")

        # get auth header
        auth_header = {
            "Authorization": f"Basic {baseAuthSecret}",
            "requesttoken": requestToken,
        }

        sciebo_root = parent_node.add_child(
            f"sciebo-{sharingToken}", None, "Sciebo Folder"
        )
        if sciebo_root is None:
            # Duplicate folder/link, nothing more to do here
            continue

        _add_sciebo_files(ctx, WEBDAV_LOCATION, sciebo_root, sharingToken, auth_header)
        ctx.sciebo_link_cache[link] = sciebo_root.clone()


def _add_sciebo_files(
    ctx: SyncContext,
    href: str,
    parent_node: Node,
    sharingToken: str,
    auth_header: dict[str, str],
    log: logging.Logger = logger,
) -> None:
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
        )
    except Exception:
        log.exception(
            "Sciebo PROPFIND failed for href %s (share %s)",
            href,
            sharingToken,
        )
        return

    if not (200 <= propfind_response.status_code < 300):
        log.warning(
            "Sciebo PROPFIND returned status %s for href %s (share %s)",
            propfind_response.status_code,
            href,
            sharingToken,
        )
        return

    # parse the response
    soup_xml = bs(propfind_response.text, features="xml")

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

        etag_value = _extract_stable_etag(resp)

        log.info(f"Sciebo response href: {new_href}")
        # get the displayname of the response
        displayname = (
            new_href.split("/")[-2]
            if new_href.endswith("/")
            else new_href.split("/")[-1]
        )
        displayname = (
            f"sciebo-{sharingToken}" if displayname == "webdav" else displayname
        )

        # check if the response is a folder
        if new_href.endswith("/"):
            # create a new node for the folder
            folder_node = parent_node.add_child(
                displayname, None, "Sciebo Folder", etag=etag_value
            )
            if folder_node is None:
                continue
            # recursive call to get all files in the folder
            _add_sciebo_files(
                ctx, new_href, folder_node, sharingToken, auth_header, log
            )
        else:
            # create a new node for the file
            parent_node.add_child(
                displayname,
                None,
                "Sciebo File",
                url=SCIEBO_URL + new_href,
                additional_info=auth_header,
                etag=etag_value,
            )


def _extract_stable_etag(response_tag: Any) -> str | None:
    # Extract a stable content hash for this item. Prefer the SHA1 checksum
    # from oc:checksums if available; fall back to the raw ETag otherwise.
    prop = response_tag.find("d:prop")
    if prop is None:
        return None

    checksums_tag = prop.find("oc:checksums")
    if checksums_tag is not None:
        for cs in checksums_tag.find_all("oc:checksum"):
            text = (cs.text or "").strip()
            if text.upper().startswith("SHA1:"):
                return text.split(":", 1)[1]

    etag_tag = prop.find("d:getetag")
    if etag_tag and etag_tag.text:
        return str(etag_tag.text).strip()

    return None
