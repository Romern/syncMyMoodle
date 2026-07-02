import hashlib
import logging
import re
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)


def local_file_matches_etag(path: Path, etag: str) -> bool:
    """Return True if the local file content matches the given ETag hash.

    We currently support strong ETags that contain a plain hex digest for MD5
    (32 chars), SHA1 (40 chars) or SHA256 (64 chars). Other formats are
    ignored and treated as non-matching.
    """
    # Extract a plausible hex digest from the ETag value, ignoring weak
    # prefixes (W/) and surrounding quotes or algorithm markers.
    match = re.search(r"([0-9a-fA-F]{32,64})", etag)
    if not match:
        return False
    hex_str = match.group(1).lower()

    algo = None
    if len(hex_str) == 32:
        algo = "md5"
    elif len(hex_str) == 40:
        algo = "sha1"
    elif len(hex_str) == 64:
        algo = "sha256"
    else:
        return False

    with path.open("rb") as f:
        digest = hashlib.file_digest(f, algo)
        return digest.hexdigest() == hex_str


def content_type_without_parameters(response):
    content_type = response.headers.get("Content-Type", "")
    return content_type.split(";", 1)[0].strip().lower()


def node_allows_html_download(node) -> bool:
    html_suffixes = {".htm", ".html", ".xhtml"}
    node_suffix = Path(str(node.name or "")).suffix.lower()
    url_suffix = Path(urllib.parse.urlparse(str(node.url or "")).path).suffix.lower()
    return node_suffix in html_suffixes or url_suffix in html_suffixes


def chunk_looks_like_html(chunk) -> bool:
    body_start = chunk.lstrip().lower()
    return bool(
        body_start.startswith(b"<!doctype html") or body_start.startswith(b"<html")
    )


def download_response_is_usable(node, response, downloadpath, log=logger) -> bool:
    if response.status_code == 204:
        log.warning(
            "Skipping download of %s from %s because the server returned no " "content",
            downloadpath,
            node.url,
        )
        return False

    if not (200 <= response.status_code < 300):
        log.warning(
            "Skipping download of %s from %s because the server returned " "HTTP %s",
            downloadpath,
            node.url,
            response.status_code,
        )
        return False

    content_type = content_type_without_parameters(response)
    if content_type in {"text/html", "application/xhtml+xml"}:
        if not node_allows_html_download(node):
            log.warning(
                "Skipping download of %s from %s because the server returned "
                "HTML instead of the expected file. This usually means the "
                "link requires a separate login or points to an error page.",
                downloadpath,
                node.url,
            )
            return False

    return True
