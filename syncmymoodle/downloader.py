import hashlib
import logging
import os
import re
import urllib.parse
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yt_dlp
from tqdm import tqdm

from syncmymoodle import course_cache, filters, pathing
from syncmymoodle.constants import HASH_ALGOS_BY_LENGTH, YOUTUBE_ID_LENGTH
from syncmymoodle.context import SyncContext

logger = logging.getLogger(__name__)

DEFAULT_BLOCK_SIZE = 1024


class FileMatch(Enum):
    """Comparison result for a local file and a remote version marker."""

    MATCH = "match"
    DIFFER = "differ"
    UNKNOWN = "unknown"


class LocalCopyState(Enum):
    """State of the existing local file when the remote may have changed."""

    UP_TO_DATE = "up_to_date"
    CLEAN = "clean"
    MODIFIED = "modified"


def classify_local_file(path: Path, marker: str | None) -> FileMatch:
    """Compare a local file against a remote ``marker``.

    Only strong markers carrying a plain MD5/SHA1/SHA256 hex digest can verify
    content. Opaque, missing, or unreadable markers are UNKNOWN.
    """
    if not marker:
        return FileMatch.UNKNOWN
    match = re.search(r"([0-9a-fA-F]{32,64})", str(marker))
    if not match:
        return FileMatch.UNKNOWN
    hex_str = match.group(1).lower()
    algo = HASH_ALGOS_BY_LENGTH.get(len(hex_str))
    if algo is None:
        return FileMatch.UNKNOWN
    try:
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, algo).hexdigest()
    except OSError:
        return FileMatch.UNKNOWN
    return FileMatch.MATCH if digest == hex_str else FileMatch.DIFFER


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


def conditional_get_confirms_unchanged(
    ctx: SyncContext,
    node: Any,
    old_etag: str,
    log: logging.Logger = logger,
) -> bool:
    """Return True when a cheap conditional GET proves the local file is current.

    Some remote nodes, notably legacy Opencast and embedded video nodes, only
    expose an ETag on the GET response. When the current scan cannot populate
    ``node.etag``, ask the server whether the cached GET ETag is still current
    before committing to a full re-download.
    """
    if not node.url:
        return False

    headers: dict[str, str] = {"If-None-Match": old_etag}
    if node.type.lower() == "sciebo file" and isinstance(node.additional_info, dict):
        headers = {**headers, **node.additional_info}

    try:
        with closing(
            ctx.require_session().get(node.url, headers=headers, stream=True)
        ) as response:
            response_etag = response.headers.get("ETag")
            if response.status_code == 304:
                node.etag = old_etag
                return True
            if 200 <= response.status_code < 300 and response_etag == old_etag:
                node.etag = response_etag
                return True
    except Exception as exc:
        log.warning("Failed to validate cached ETag for %s: %s", node.url, exc)
    return False


@dataclass
class DownloadDecision:
    """Outcome of the change-detection step for a single file."""

    skip: bool
    conflict: bool = False


def remote_unchanged(
    ctx: SyncContext,
    node: Any,
    old_node: Any,
    cached_timemodified: Any,
    log: logging.Logger = logger,
) -> bool:
    """Whether the remote file is provably unchanged since our last download."""
    old_etag = getattr(old_node, "etag", None)
    node_etag = getattr(node, "etag", None)

    if cached_timemodified is not None:
        return bool(node.timemodified == cached_timemodified)

    if old_etag and node_etag == old_etag:
        return True
    if (
        old_etag
        and node_etag is None
        and conditional_get_confirms_unchanged(ctx, node, old_etag, log)
    ):
        return True
    return False


def assess_local_copy(
    ctx: SyncContext,
    node: Any,
    downloadpath: Path,
    old_node: Any,
    cached_timemodified: Any,
    log: logging.Logger = logger,
) -> LocalCopyState:
    """Classify the on-disk file when the remote may have changed."""
    old_etag = getattr(old_node, "etag", None) if old_node is not None else None
    old_content_hash = (
        getattr(old_node, "content_hash", None) if old_node is not None else None
    )
    verdict = classify_local_file(downloadpath, old_content_hash or old_etag)
    if verdict is FileMatch.MATCH:
        return LocalCopyState.CLEAN
    if verdict is FileMatch.DIFFER:
        return LocalCopyState.MODIFIED

    if cached_timemodified is not None:
        try:
            if int(downloadpath.stat().st_mtime) != int(cached_timemodified):
                return LocalCopyState.MODIFIED
            return LocalCopyState.CLEAN
        except (OSError, ValueError):
            return LocalCopyState.MODIFIED

    remote_etag = getattr(node, "etag", None)
    if remote_etag is None and node.url:
        try:
            head_resp = ctx.require_session().head(node.url, allow_redirects=True)
            remote_etag = head_resp.headers.get("ETag")
        except Exception:
            remote_etag = None

    if classify_local_file(downloadpath, remote_etag) is FileMatch.MATCH:
        node.etag = remote_etag
        if getattr(node, "timemodified", None) is not None:
            try:
                ts = int(node.timemodified)
                os.utime(downloadpath, (ts, ts))
            except (OSError, OverflowError, ValueError):
                pass
        return LocalCopyState.UP_TO_DATE
    return LocalCopyState.MODIFIED


def decide_download(
    ctx: SyncContext,
    node: Any,
    downloadpath: Path,
    log: logging.Logger = logger,
) -> DownloadDecision:
    """Decide whether ``node`` must be (re)downloaded and whether the local copy
    is user-modified.
    """
    if not downloadpath.exists():
        return DownloadDecision(skip=False)
    if not ctx.config.updatefiles:
        return DownloadDecision(skip=True)

    old_node = course_cache.get_old_node_for(ctx, node, log)
    if old_node is not None and not getattr(old_node, "is_downloaded", False):
        old_node = None
    cached_timemodified = (
        getattr(old_node, "timemodified", None) if old_node is not None else None
    )

    if old_node is not None and remote_unchanged(
        ctx, node, old_node, cached_timemodified, log
    ):
        return DownloadDecision(skip=True)

    verdict = assess_local_copy(
        ctx, node, downloadpath, old_node, cached_timemodified, log
    )
    if verdict is LocalCopyState.UP_TO_DATE:
        return DownloadDecision(skip=True)
    return DownloadDecision(skip=False, conflict=verdict is LocalCopyState.MODIFIED)


def download_file(
    ctx: SyncContext,
    node: Any,
    log: logging.Logger = logger,
) -> bool:
    """Download file with progress bar if it isn't already downloaded."""
    downloadpath = pathing.get_sanitized_node_path(node, Path(ctx.config.basedir))

    if filters.should_skip_url(ctx.config, node.url, f"{node.type} file", log):
        return True

    # Exclusions must not trigger conflict handling or temp-file writes.
    if node.name.split(".")[-1] in ctx.config.exclude_filetypes:
        return True
    if any(fnmatchcase(node.name, pattern) for pattern in ctx.config.exclude_files):
        return True

    if ctx.downloaded_paths is None:
        ctx.downloaded_paths = set()
    elif downloadpath in ctx.downloaded_paths:
        return True

    decision = decide_download(ctx, node, downloadpath, log)
    if decision.skip:
        return True

    conflict_rename_pending = False
    if decision.conflict:
        conflict_mode = ctx.config.update_files_conflict
        if conflict_mode not in {"rename", "keep", "none", "overwrite"}:
            conflict_mode = "rename"
        if conflict_mode in {"keep", "none"}:
            log.info(
                "Detected local changes for %s, skipping Moodle update "
                "due to update_files_conflict=%s",
                downloadpath,
                conflict_mode,
            )
            return True
        if conflict_mode == "rename":
            conflict_rename_pending = True

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

        if not download_response_is_usable(node, response, downloadpath, log):
            return False

        content = response.iter_content(DEFAULT_BLOCK_SIZE)
        first_chunk = next((chunk for chunk in content if chunk), b"")
        if (
            first_chunk
            and chunk_looks_like_html(first_chunk)
            and not node_allows_html_download(node)
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
            conflict_path = pathing.make_conflict_path(downloadpath)
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
        # Record a content hash of exactly the bytes we just downloaded, so the
        # next run can detect genuine local modifications even when the remote
        # only offers an opaque ETag (e.g. Sciebo/WebDAV). We hash the file we
        # just wrote (untouched by the user at this point), never a pre-existing
        # file, so a later user edit is never mistaken for our own download.
        try:
            with downloadpath.open("rb") as fh:
                node.content_hash = hashlib.file_digest(fh, "sha256").hexdigest()
        except OSError:
            pass
        # Align the local mtime with Moodle's timemodified to detect local
        # changes on subsequent runs.
        if getattr(node, "timemodified", None) is not None:
            try:
                ts = int(node.timemodified)
                os.utime(downloadpath, (ts, ts))
            except (OSError, OverflowError, ValueError):
                # If updating timestamps fails, fall back to the current time.
                pass
        # Persist a response ETag only when discovery did not already provide a
        # remote version marker. Sciebo/WebDAV can expose one marker through
        # PROPFIND and a different ETag on GET; the next scan compares against
        # the PROPFIND marker, so replacing it here would force re-downloads on
        # every run.
        if etag_header is not None and getattr(node, "etag", None) is None:
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

    download_node_tree(ctx, ctx.root_node, log)


def download_node_tree(
    ctx: SyncContext,
    cur_node: Any,
    log: logging.Logger = logger,
) -> None:
    if len(cur_node.children) == 0:
        if cur_node.url and not cur_node.is_downloaded:
            if cur_node.type == "Youtube":
                try:
                    scan_and_download_youtube(ctx, cur_node, log)
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
                    if download_file(ctx, cur_node, log):
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
                    if download_file(ctx, cur_node, log):
                        cur_node.is_downloaded = True
                except Exception:
                    log.exception(f"Failed to download the module {cur_node}")
        return

    for child in cur_node.children:
        download_node_tree(ctx, child, log)


def scan_and_download_youtube(
    ctx: SyncContext,
    node: Any,
    log: logging.Logger = logger,
) -> bool:
    """Download Youtube-Videos using yt_dlp."""
    path = pathing.get_sanitized_node_path(node.parent, Path(ctx.config.basedir))
    link = node.url
    if filters.should_skip_url(ctx.config, link, "YouTube link", log):
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
