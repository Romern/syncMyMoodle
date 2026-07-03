import hashlib
import logging
import os
import re
import urllib.parse
from contextlib import closing
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable

import yt_dlp
from tqdm import tqdm

from syncmymoodle.constants import YOUTUBE_ID_LENGTH
from syncmymoodle.context import SyncContext

logger = logging.getLogger(__name__)


@dataclass
class DownloadServices:
    chunk_looks_like_html: Callable[[bytes], bool]
    download_response_is_usable: Callable[[Any, Any, Path], bool]
    get_old_node_for: Callable[[Any], Any]
    get_sanitized_node_path: Callable[[Any], Path]
    local_file_matches_etag: Callable[[Path, str], bool]
    make_conflict_path: Callable[[Path], Path]
    node_allows_html_download: Callable[[Any], bool]
    should_skip_url: Callable[[str | None, str], bool]


@dataclass
class DownloadTreeServices:
    download_file: Callable[[Any], bool]
    scan_and_download_youtube: Callable[[Any], bool]


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


def content_type_without_parameters(response: Any) -> str:
    content_type = str(response.headers.get("Content-Type", ""))
    return content_type.split(";", 1)[0].strip().lower()


def node_allows_html_download(node: Any) -> bool:
    html_suffixes = {".htm", ".html", ".xhtml"}
    node_suffix = Path(str(node.name or "")).suffix.lower()
    url_suffix = Path(urllib.parse.urlparse(str(node.url or "")).path).suffix.lower()
    return node_suffix in html_suffixes or url_suffix in html_suffixes


def chunk_looks_like_html(chunk: bytes) -> bool:
    body_start = chunk.lstrip().lower()
    return bool(
        body_start.startswith(b"<!doctype html") or body_start.startswith(b"<html")
    )


def download_response_is_usable(
    node: Any,
    response: Any,
    downloadpath: Path,
    log: logging.Logger = logger,
) -> bool:
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


def download_file(
    ctx: SyncContext,
    node: Any,
    services: DownloadServices,
    block_size: int,
    log: logging.Logger = logger,
) -> bool:
    """Download file with progress bar if it isn't already downloaded."""
    downloadpath = services.get_sanitized_node_path(node)

    if services.should_skip_url(node.url, f"{node.type} file"):
        return True

    # Respect filetype/name exclusions up front so that excluded files never
    # trigger conflict handling, displace local files, or create temp files.
    if node.name.split(".")[-1] in ctx.config.exclude_filetypes:
        return True
    if any(fnmatchcase(node.name, pattern) for pattern in ctx.config.exclude_files):
        return True

    # If we already downloaded this path during the current run, skip any
    # further processing. This avoids duplicate downloads and spurious
    # conflicts when the same remote file appears multiple times in the node
    # tree (e.g. Sciebo links reused in a course).
    if ctx.downloaded_paths is None:
        # Initialise on first use to keep __init__ simple.
        ctx.downloaded_paths = set()
    elif downloadpath in ctx.downloaded_paths:
        return True

    # Decide whether we need to (re-)download the file at all
    cached_timemodified = None
    old_node = None
    conflict_rename_pending = False
    if downloadpath.exists():
        if not ctx.config.updatefiles:
            return True

        # Try to find a cached node for this file from the per-course cache.
        old_node = services.get_old_node_for(node)
        # Only trust the cached version markers when the previous run actually
        # downloaded the file. Otherwise an update that failed last time (e.g.
        # an expired session) gets cached with Moodle's new timemodified and
        # would be skipped forever, leaving a stale file. Treat a
        # non-downloaded cache entry as if there were no cache at all.
        if old_node is not None and not getattr(old_node, "is_downloaded", False):
            old_node = None
        if old_node is not None:
            cached_timemodified = getattr(old_node, "timemodified", None)
            old_etag = getattr(old_node, "etag", None)
            # If Moodle did not change the file, skip re-download. Only when
            # timemodified is meaningful: Sciebo files have no timemodified
            # (always None), so this must fall through to the etag check below
            # instead of treating None == None as "unchanged".
            if cached_timemodified is not None and (
                node.timemodified == cached_timemodified
            ):
                return True
            # For Sciebo, we use the etag from the previous run as the remote
            # version marker. If it matches the current etag from the PROPFIND
            # response, the remote file has not changed.
            if (
                cached_timemodified is None
                and old_etag
                and getattr(node, "etag", None) == old_etag
            ):
                # Additionally, on the first run with a cache, the local file
                # may already match this etag (e.g. previously downloaded
                # manually). If so, we can safely skip any download.
                if services.local_file_matches_etag(downloadpath, old_etag):
                    return True

        # At this point, either there is no cache for this course/path, or
        # Moodle reports a different modification time. This means the remote
        # file might have changed.

        # Check for potential local modifications since the last sync to avoid
        # silently overwriting user changes.
        conflict_mode = ctx.config.update_files_conflict
        if conflict_mode not in {"rename", "keep", "none", "overwrite"}:
            conflict_mode = "rename"

        local_conflict = False
        old_etag = getattr(old_node, "etag", None) if old_node is not None else None
        etag_check_failed = False
        if old_etag:
            # Prefer using the old ETag (hash) to detect whether the local file
            # still matches the previously downloaded version.
            try:
                if not services.local_file_matches_etag(downloadpath, old_etag):
                    local_conflict = True
            except Exception:
                # A faulty/unusable ETag cache is treated as if we had no
                # cached ETag at all: fall back to the timestamp/HEAD heuristic
                # below to decide whether this is a conflict.
                etag_check_failed = True

        if not old_etag or etag_check_failed:
            if cached_timemodified is not None:
                # Fallback: compare local mtime with the previous Moodle timestamp.
                try:
                    local_mtime = int(downloadpath.stat().st_mtime)
                    if local_mtime != int(cached_timemodified):
                        local_conflict = True
                except (OSError, ValueError):
                    local_conflict = True
            else:
                # No previous etag and no previous timemodified: this usually
                # means the file existed before we ever cached it. Before we
                # treat this as a conflict, try to see if the local file already
                # matches the *current* remote content using the ETag from
                # either the Sciebo PROPFIND or a Moodle HEAD request.
                remote_etag = getattr(node, "etag", None)
                if remote_etag is None and node.url:
                    try:
                        head_resp = ctx.require_session().head(
                            node.url, allow_redirects=True
                        )
                        remote_etag = head_resp.headers.get("ETag")
                    except Exception:
                        remote_etag = None

                if remote_etag and services.local_file_matches_etag(
                    downloadpath, remote_etag
                ):
                    # Local file already equals the current remote content, so
                    # there is no conflict and no need to download again.
                    node.etag = remote_etag
                    if getattr(node, "timemodified", None) is not None:
                        try:
                            ts = int(node.timemodified)
                            os.utime(downloadpath, (ts, ts))
                        except (OSError, OverflowError, ValueError):
                            pass
                    return True

                # At this point we know the local file differs from the current
                # remote version (or we couldn't verify), and we have no prior
                # cached state. Treat this as a potential conflict to avoid
                # silently overwriting user changes.
                local_conflict = True

        if local_conflict:
            if conflict_mode in {"keep", "none"}:
                # Keep the locally modified file and skip updating from Moodle
                log.info(
                    "Detected local changes for %s, skipping Moodle update "
                    "due to update_files_conflict=%s",
                    downloadpath,
                    conflict_mode,
                )
                return True
            if conflict_mode == "rename":
                # Defer moving the locally modified file aside until the
                # replacement has been fully downloaded, so an aborted or failed
                # download (e.g. an expired session returning an HTML error
                # page) never leaves the canonical path empty.
                conflict_rename_pending = True
            # conflict_mode == "overwrite": fall through and overwrite

    # Hidden, namespaced temp/sidecar names so we never resume from or
    # overwrite a file the user happens to own. The sidecar records the ETag a
    # partial download was fetched against.
    tmp_downloadpath = downloadpath.parent / f".{downloadpath.name}.smmpart"
    etag_sidecar = tmp_downloadpath.with_name(tmp_downloadpath.name + ".etag")

    # Only resume a previous partial when we recorded the ETag it was fetched
    # against, so we can ask the server (via If-Range) to confirm the remote
    # content is unchanged. Without that proof a blind range request could
    # splice bytes from a newer version onto an older partial and silently
    # corrupt the file.
    resume_size = 0
    partial_etag: str | None = None
    header = dict()
    if tmp_downloadpath.exists():
        if etag_sidecar.exists():
            try:
                partial_etag = etag_sidecar.read_text(encoding="utf-8").strip()
            except OSError:
                partial_etag = None
        if partial_etag:
            resume_size = tmp_downloadpath.stat().st_size
            header = {"Range": f"bytes={resume_size}-", "If-Range": partial_etag}
        else:
            # Cannot validate the partial; discard it and start fresh.
            tmp_downloadpath.unlink(missing_ok=True)
            etag_sidecar.unlink(missing_ok=True)
    if node.type.lower() == "sciebo file":
        header = {**header, **node.additional_info}

    with closing(
        ctx.require_session().get(node.url, headers=header, stream=True)
    ) as response:
        etag_header = response.headers.get("ETag")

        if resume_size:
            # The remote content differs from our partial when the server
            # ignores the range (any non-206) or cannot prove that the returned
            # tail belongs to the same ETag as the saved partial.
            valid_resume = response.status_code == 206 and etag_header == partial_etag
            version_changed = not valid_resume
            if version_changed:
                resume_size = 0
                tmp_downloadpath.unlink(missing_ok=True)
                etag_sidecar.unlink(missing_ok=True)
                if response.status_code == 206:
                    # This 206 body is only a tail, and without an exact ETag
                    # match it cannot be safely appended. Restart fresh on the
                    # next run.
                    return False

        if not services.download_response_is_usable(node, response, downloadpath):
            return False

        content = response.iter_content(block_size)
        first_chunk = next((chunk for chunk in content if chunk), b"")
        if (
            first_chunk
            and services.chunk_looks_like_html(first_chunk)
            and not services.node_allows_html_download(node)
        ):
            log.warning(
                "Skipping download of %s from %s because the response body "
                "starts with HTML instead of the expected file. This usually "
                "means the link requires a separate login or points to an "
                "error page.",
                downloadpath,
                node.url,
            )
            return False

        print(f"Downloading {downloadpath} [{node.type}]")
        total_size_in_bytes = int(response.headers.get("content-length", 0)) + max(
            resume_size, 0
        )
        progress_bar = tqdm(total=total_size_in_bytes, unit="iB", unit_scale=True)
        if resume_size:
            progress_bar.update(resume_size)
        downloadpath.parent.mkdir(parents=True, exist_ok=True)
        # Record the ETag this partial is being fetched against so an
        # interrupted download can be safely resumed next time.
        if etag_header:
            try:
                etag_sidecar.write_text(etag_header, encoding="utf-8")
            except OSError:
                pass
        mode = "ab" if resume_size else "wb"
        with tmp_downloadpath.open(mode) as file:
            if first_chunk:
                progress_bar.update(len(first_chunk))
                file.write(first_chunk)
            for data in content:
                progress_bar.update(len(data))
                file.write(data)
        progress_bar.close()

        # The replacement is now fully on disk. Only at this point do we move a
        # conflicting local file aside, so a failure above never empties the
        # canonical path.
        if conflict_rename_pending:
            conflict_path = services.make_conflict_path(downloadpath)
            try:
                downloadpath.rename(conflict_path)
                log.warning(
                    "Detected local changes for %s, moved to %s before "
                    "installing the updated file from Moodle",
                    downloadpath,
                    conflict_path,
                )
            except OSError:
                log.exception(
                    "Failed to move locally modified file %s to %s; keeping "
                    "it and discarding the downloaded update to avoid data loss",
                    downloadpath,
                    conflict_path,
                )
                tmp_downloadpath.unlink(missing_ok=True)
                etag_sidecar.unlink(missing_ok=True)
                return True

        os.replace(tmp_downloadpath, downloadpath)
        etag_sidecar.unlink(missing_ok=True)
        # Align the local mtime with Moodle's timemodified to detect local
        # changes on subsequent runs.
        if getattr(node, "timemodified", None) is not None:
            try:
                ts = int(node.timemodified)
                os.utime(downloadpath, (ts, ts))
            except (OSError, OverflowError, ValueError):
                # If updating timestamps fails, fall back to the current time.
                pass
        # Persist the ETag of the downloaded file on the node so it can be used
        # on the next run to detect local modifications.
        if etag_header is not None:
            try:
                node.etag = etag_header
            except Exception:
                # If for some reason we cannot set it, just ignore.
                pass
        # Remember that we downloaded this path during the current run.
        ctx.downloaded_paths.add(downloadpath)
        return True


def download_all_files(
    ctx: SyncContext,
    services: DownloadTreeServices,
    log: logging.Logger = logger,
) -> None:
    if not ctx.session:
        raise Exception("You need to login() first.")
    if not ctx.wstoken:
        raise Exception("You need to get_moodle_wstoken() first.")
    if not ctx.user_id:
        raise Exception("You need to get_userid() first.")
    if not ctx.root_node:
        raise Exception("You need to sync() first.")

    download_node_tree(ctx.root_node, services, log)


def download_node_tree(
    cur_node: Any,
    services: DownloadTreeServices,
    log: logging.Logger = logger,
) -> None:
    if len(cur_node.children) == 0:
        if cur_node.url and not cur_node.is_downloaded:
            if cur_node.type == "Youtube":
                try:
                    services.scan_and_download_youtube(cur_node)
                    cur_node.is_downloaded = True
                except Exception:
                    log.exception(f"Failed to download the module {cur_node}")
                    log.error(
                        "This could be caused by an out of date yt-dlp version. Try upgrading yt-dlp through pip or your package manager."
                    )
            elif cur_node.type == "Opencast":
                try:
                    # download Opencast videos
                    if ".mp4" not in cur_node.name:
                        if cur_node.name is not None and cur_node.name != "":
                            cur_node.name += ".mp4"
                        else:
                            cur_node.name = cur_node.url.split("/")[-1]
                    if services.download_file(cur_node):
                        cur_node.is_downloaded = True
                except Exception:
                    log.exception(f"Failed to download the module {cur_node}")
            elif cur_node.type == "Quiz":
                log.warning(
                    "Skipping quiz PDF generation for %s because it is disabled "
                    "for security.",
                    cur_node.name,
                )
            else:
                try:
                    if services.download_file(cur_node):
                        cur_node.is_downloaded = True
                except Exception:
                    log.exception(f"Failed to download the module {cur_node}")
        return

    for child in cur_node.children:
        download_node_tree(child, services, log)


def scan_and_download_youtube(
    node: Any,
    get_sanitized_node_path: Callable[[Any], Path],
    should_skip_url: Callable[[str | None, str], bool],
) -> bool:
    """Download Youtube-Videos using yt_dlp."""
    path = get_sanitized_node_path(node.parent)
    link = node.url
    if should_skip_url(link, "YouTube link"):
        return True
    if path.exists():
        if any(link[-YOUTUBE_ID_LENGTH:] in f.name for f in path.iterdir()):
            return False
    ydl_opts = {
        "outtmpl": "{}/%(title)s-%(id)s.%(ext)s".format(path),
        "ignoreerrors": True,
        "nooverwrites": True,
        "retries": 15,
        "match_filter": yt_dlp.match_filter_func("!is_live"),
    }
    path.mkdir(parents=True, exist_ok=True)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([link])
    return True


def download_quiz(node: Any, log: logging.Logger = logger) -> bool:
    log.warning(
        "Quiz PDF generation is disabled until the pdfkit/wkhtmltopdf "
        "renderer is replaced with a safer implementation."
    )
    return False
