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

from syncmymoodle import course_cache, filters, links, pathing, quiz
from syncmymoodle.constants import (
    DEFAULT_BLOCK_SIZE,
    HASH_ALGOS_BY_LENGTH,
)
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import (
    HTML_CONTENT_TYPES,
    content_length,
    content_type_without_parameters,
)
from syncmymoodle.node import Node, RemoteMarkerKind

logger = logging.getLogger(__name__)


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


class ConflictAction(Enum):
    DOWNLOAD = "download"
    RENAME_LOCAL = "rename_local"
    SKIP = "skip"


@dataclass
class TransferPlan:
    tmp_path: Path
    etag_sidecar: Path
    headers: dict[str, str]
    resume_size: int = 0
    partial_etag: str | None = None

    def discard_partial(self) -> None:
        self.resume_size = 0
        self.partial_etag = None
        self.tmp_path.unlink(missing_ok=True)
        self.etag_sidecar.unlink(missing_ok=True)


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
            "Skipping download of %s from %s because the server returned no content",
            downloadpath,
            node.url,
        )
        return False

    if not (200 <= response.status_code < 300):
        log.warning(
            "Skipping download of %s from %s because the server returned HTTP %s",
            downloadpath,
            node.url,
            response.status_code,
        )
        return False

    content_type = content_type_without_parameters(response)
    if content_type in HTML_CONTENT_TYPES:
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
    node: Node,
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
    if node.download_headers:
        headers = {**headers, **node.download_headers}

    try:
        with closing(
            ctx.require_session().get(node.url, headers=headers, stream=True)
        ) as response:
            response_etag = response.headers.get("ETag")
            if response.status_code == 304:
                node.etag = old_etag
                node.etag_kind = RemoteMarkerKind.OPAQUE
                return True
            if 200 <= response.status_code < 300 and response_etag == old_etag:
                node.etag = response_etag
                node.etag_kind = RemoteMarkerKind.OPAQUE
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
    node: Node,
    old_node: Node,
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


def align_mtime_with_timemodified(node: Node, downloadpath: Path) -> None:
    """Set the local file's mtime to Moodle's timemodified.

    Later runs use the timestamp to detect local changes
    """
    if getattr(node, "timemodified", None) is None:
        return
    try:
        ts = int(node.timemodified)
        os.utime(downloadpath, (ts, ts))
    except (OSError, OverflowError, ValueError):
        pass


def local_verification_marker(old_node: Node | None) -> str | None:
    if old_node is None:
        return None
    if old_node.content_hash:
        return old_node.content_hash
    if old_node.etag and old_node.etag_kind in (None, RemoteMarkerKind.CONTENT_HASH):
        return old_node.etag
    return None


def assess_local_copy(
    node: Node,
    downloadpath: Path,
    old_node: Node | None,
    cached_timemodified: Any,
) -> LocalCopyState:
    """Classify the on-disk file when the remote may have changed."""
    verdict = classify_local_file(downloadpath, local_verification_marker(old_node))
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
    remote_etag_kind = node.etag_kind
    if (
        remote_etag_kind == RemoteMarkerKind.CONTENT_HASH
        and classify_local_file(downloadpath, remote_etag) is FileMatch.MATCH
    ):
        node.etag = remote_etag
        node.etag_kind = remote_etag_kind
        align_mtime_with_timemodified(node, downloadpath)
        return LocalCopyState.UP_TO_DATE
    return LocalCopyState.MODIFIED


def decide_download(
    ctx: SyncContext,
    node: Node,
    downloadpath: Path,
    log: logging.Logger = logger,
) -> DownloadDecision:
    """Decide whether ``node`` must be (re)downloaded and whether the local copy
    is user-modified.
    """
    if not downloadpath.exists():
        return DownloadDecision(skip=False)
    if not ctx.config.update_files:
        return DownloadDecision(skip=True)

    old_node = course_cache.get_old_node_for(ctx, node, log)
    if old_node is not None and not old_node.is_handled:
        old_node = None
    cached_timemodified = (
        getattr(old_node, "timemodified", None) if old_node is not None else None
    )

    if old_node is not None and remote_unchanged(
        ctx, node, old_node, cached_timemodified, log
    ):
        return DownloadDecision(skip=True)

    verdict = assess_local_copy(node, downloadpath, old_node, cached_timemodified)
    if verdict is LocalCopyState.UP_TO_DATE:
        return DownloadDecision(skip=True)
    return DownloadDecision(skip=False, conflict=verdict is LocalCopyState.MODIFIED)


def should_skip_before_decision(
    ctx: SyncContext, node: Node, downloadpath: Path, log: logging.Logger = logger
) -> bool:
    if filters.should_skip_url(ctx.config, node.url, f"{node.type} file", log):
        return True
    if node.name.split(".")[-1] in ctx.config.exclude_filetypes:
        return True
    if any(fnmatchcase(node.name, pattern) for pattern in ctx.config.exclude_files):
        return True
    return downloadpath in ctx.downloaded_paths


def conflict_action(
    ctx: SyncContext,
    decision: DownloadDecision,
    downloadpath: Path,
    log: logging.Logger = logger,
) -> ConflictAction:
    if not decision.conflict:
        return ConflictAction.DOWNLOAD

    conflict_mode = ctx.config.conflict_handling
    if conflict_mode == "keep":
        log.info(
            "Detected local changes for %s, skipping Moodle update "
            "due to conflict_handling=%s",
            downloadpath,
            conflict_mode,
        )
        return ConflictAction.SKIP
    if conflict_mode == "rename":
        return ConflictAction.RENAME_LOCAL
    return ConflictAction.DOWNLOAD


def size_limits_configured(ctx: SyncContext) -> bool:
    return bool(ctx.config.max_file_size or ctx.config.min_file_size)


def human_readable_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        value /= 1024
        if value < 1024 or unit == "PiB":
            if value.is_integer():
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
    raise AssertionError("unreachable")


def size_limit_violation(ctx: SyncContext, size: int) -> str | None:
    """Which of filters.max_file_size/min_file_size ``size`` violates, if any."""
    max_size = ctx.config.max_file_size
    if max_size and size > max_size:
        return f"exceeds max_file_size ({human_readable_size(max_size)})"
    min_size = ctx.config.min_file_size
    if min_size and size < min_size:
        return f"is below min_file_size ({human_readable_size(min_size)})"
    return None


def known_remote_size_violates_limit(
    ctx: SyncContext,
    node: Node,
    downloadpath: Path,
    log: logging.Logger = logger,
) -> bool:
    if not size_limits_configured(ctx) or node.remote_size is None:
        return False
    violation = size_limit_violation(ctx, node.remote_size)
    if violation is None:
        return False
    log.warning(
        "Skipping download of %s because its known size (%s) %s",
        downloadpath,
        human_readable_size(node.remote_size),
        violation,
    )
    return True


def download_violates_size_limits(
    ctx: SyncContext,
    node: Node,
    response: Any,
    resume_size: int,
    downloadpath: Path,
    log: logging.Logger = logger,
) -> bool:
    """Whether the response reports a size outside the configured size limits.

    Best-effort: responses without a Content-Length header are not limited.
    """
    total_size = content_length(response, resume_size)
    if total_size is None:
        return False
    node.remote_size = total_size
    if not size_limits_configured(ctx):
        return False
    violation = size_limit_violation(ctx, total_size)
    if violation is None:
        return False
    log.warning(
        "Skipping download of %s because its size (%s) %s",
        downloadpath,
        human_readable_size(total_size),
        violation,
    )
    return True


def prepare_transfer_plan(node: Node, downloadpath: Path) -> TransferPlan:
    tmp_path = pathing.with_windows_extended_length_prefix(
        downloadpath.parent / f".{downloadpath.name}.smmpart"
    )
    etag_sidecar = pathing.with_windows_extended_length_prefix(
        tmp_path.with_name(tmp_path.name + ".etag")
    )
    plan = TransferPlan(tmp_path=tmp_path, etag_sidecar=etag_sidecar, headers={})

    if tmp_path.exists():
        if etag_sidecar.exists():
            try:
                plan.partial_etag = etag_sidecar.read_text(encoding="utf-8").strip()
            except OSError:
                plan.partial_etag = None
        if plan.partial_etag:
            plan.resume_size = tmp_path.stat().st_size
            plan.headers = {
                "Range": f"bytes={plan.resume_size}-",
                "If-Range": plan.partial_etag,
            }
        else:
            plan.discard_partial()

    if node.download_headers:
        plan.headers = {**plan.headers, **node.download_headers}
    return plan


def validate_resume_response(response: Any, transfer: TransferPlan) -> bool:
    if not transfer.resume_size:
        return True

    etag_header = response.headers.get("ETag")
    valid_resume = response.status_code == 206 and etag_header == transfer.partial_etag
    if valid_resume:
        return True

    was_partial_response = response.status_code == 206
    transfer.discard_partial()
    return not was_partial_response


def response_body_is_usable(
    node: Node,
    first_chunk: bytes,
    downloadpath: Path,
    log: logging.Logger = logger,
) -> bool:
    if not first_chunk:
        return True
    if not chunk_looks_like_html(first_chunk):
        return True
    if node_allows_html_download(node):
        return True

    log.warning(
        "Skipping download of %s from %s because the response body starts "
        "with HTML instead of the expected file. This usually means the link "
        "requires a separate login or points to an error page.",
        downloadpath,
        node.url,
    )
    return False


def write_response_body(
    node: Node,
    response: Any,
    transfer: TransferPlan,
    downloadpath: Path,
    content: Any,
    first_chunk: bytes,
) -> None:
    print(f"Downloading {downloadpath} [{node.type}]")
    total_size_in_bytes = int(response.headers.get("content-length", 0)) + max(
        transfer.resume_size, 0
    )
    progress_bar = tqdm(total=total_size_in_bytes, unit="iB", unit_scale=True)
    try:
        if transfer.resume_size:
            progress_bar.update(transfer.resume_size)
        downloadpath.parent.mkdir(parents=True, exist_ok=True)

        etag_header = response.headers.get("ETag")
        if etag_header:
            try:
                transfer.etag_sidecar.write_text(etag_header, encoding="utf-8")
            except OSError:
                pass

        mode = "ab" if transfer.resume_size else "wb"
        with transfer.tmp_path.open(mode) as file:
            if first_chunk:
                progress_bar.update(len(first_chunk))
                file.write(first_chunk)
            for data in content:
                progress_bar.update(len(data))
                file.write(data)
    finally:
        progress_bar.close()


def install_downloaded_file(
    downloadpath: Path,
    transfer: TransferPlan,
    action: ConflictAction,
    log: logging.Logger = logger,
) -> bool:
    if action == ConflictAction.RENAME_LOCAL:
        conflict_path = pathing.make_conflict_path(downloadpath)
        try:
            downloadpath.rename(conflict_path)
            log.warning(
                "Detected local changes for %s, moved to %s before installing "
                "the updated file from Moodle",
                downloadpath,
                conflict_path,
            )
        except OSError:
            log.exception(
                "Failed to move locally modified file %s to %s; keeping it "
                "and discarding the downloaded update to avoid data loss",
                downloadpath,
                conflict_path,
            )
            transfer.discard_partial()
            return False

    os.replace(transfer.tmp_path, downloadpath)
    transfer.etag_sidecar.unlink(missing_ok=True)
    return True


def record_download_metadata(
    node: Node,
    downloadpath: Path,
    etag_header: str | None,
) -> None:
    try:
        with downloadpath.open("rb") as fh:
            node.content_hash = hashlib.file_digest(fh, "sha256").hexdigest()
    except OSError:
        pass

    align_mtime_with_timemodified(node, downloadpath)

    if etag_header is not None and node.etag is None:
        node.etag = etag_header
        node.etag_kind = RemoteMarkerKind.OPAQUE


def download_file(
    ctx: SyncContext,
    node: Node,
    log: logging.Logger = logger,
) -> bool:
    """Download file with progress bar if it isn't already downloaded."""
    downloadpath = pathing.get_sanitized_node_path(
        node, Path(ctx.config.sync_directory)
    )

    if not node.url:
        return False

    if should_skip_before_decision(ctx, node, downloadpath, log):
        return True
    if known_remote_size_violates_limit(ctx, node, downloadpath, log):
        return True

    decision = decide_download(ctx, node, downloadpath, log)
    if decision.skip:
        return True

    action = conflict_action(ctx, decision, downloadpath, log)
    if action == ConflictAction.SKIP:
        return True

    if ctx.config.dry_run and not size_limits_configured(ctx):
        print(f"Would download {downloadpath} [{node.type}]")
        return True

    transfer = None if ctx.config.dry_run else prepare_transfer_plan(node, downloadpath)
    headers = (
        dict(node.download_headers)
        if transfer is None and node.download_headers
        else transfer.headers
        if transfer is not None
        else {}
    )
    with closing(
        ctx.require_session().get(node.url, headers=headers, stream=True)
    ) as response:
        etag_header = response.headers.get("ETag")

        if transfer is not None and not validate_resume_response(response, transfer):
            return False

        resume_size = transfer.resume_size if transfer is not None else 0
        if not download_response_is_usable(node, response, downloadpath, log):
            return True if ctx.config.dry_run else False
        if download_violates_size_limits(
            ctx, node, response, resume_size, downloadpath, log
        ):
            return True

        if ctx.config.dry_run:
            print(f"Would download {downloadpath} [{node.type}]")
            return True

        assert transfer is not None
        content = response.iter_content(DEFAULT_BLOCK_SIZE)
        first_chunk = next((chunk for chunk in content if chunk), b"")
        if not response_body_is_usable(node, first_chunk, downloadpath, log):
            return False

        write_response_body(
            node, response, transfer, downloadpath, content, first_chunk
        )
        if not install_downloaded_file(downloadpath, transfer, action, log):
            return True
        record_download_metadata(node, downloadpath, etag_header)
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
    cur_node: Node,
    log: logging.Logger = logger,
) -> None:
    if len(cur_node.children) == 0:
        if cur_node.url and not cur_node.is_handled:
            if cur_node.type == "Youtube":
                try:
                    scan_and_download_youtube(ctx, cur_node, log)
                    cur_node.mark_handled()
                except Exception:
                    log.exception(f"Failed to download the module {cur_node}")
                    log.error(
                        "This could be caused by an out of date yt-dlp version. Try upgrading yt-dlp through pip or "
                        "your package manager."
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
                        cur_node.mark_handled()
                except Exception:
                    log.exception(f"Failed to download the module {cur_node}")
            elif cur_node.type == "Quiz":
                try:
                    if quiz.download_quiz(ctx, cur_node, log):
                        cur_node.mark_handled()
                except Exception:
                    log.exception(f"Failed to download the module {cur_node}")
            else:
                try:
                    if download_file(ctx, cur_node, log):
                        cur_node.mark_handled()
                except Exception:
                    log.exception(f"Failed to download the module {cur_node}")
        return

    for child in cur_node.children:
        download_node_tree(ctx, child, log)


def scan_and_download_youtube(
    ctx: SyncContext,
    node: Node,
    log: logging.Logger = logger,
) -> bool:
    """Download Youtube-Videos using yt_dlp."""
    if node.parent is None or node.url is None:
        return False
    path = pathing.get_sanitized_node_path(node.parent, Path(ctx.config.sync_directory))
    link = node.url
    if filters.should_skip_url(ctx.config, link, "YouTube link", log):
        return True
    video_id = links.youtube_video_id_from_node(node)
    if path.exists():
        if video_id and any(video_id in f.name for f in path.iterdir()):
            return False
    if ctx.config.dry_run and not size_limits_configured(ctx):
        print(f"Would download YouTube video {link} to {path} [Youtube]")
        return True
    if cached_youtube_size_violates_limit(ctx, node, path, log):
        return True
    outtmpl = pathing.with_windows_extended_length_prefix(
        path / "%(title)s-%(id)s.%(ext)s",
        force=True,
    )
    ydl_opts = {
        "outtmpl": os.fspath(outtmpl),
        "ignoreerrors": True,
        "nooverwrites": True,
        "retries": 15,
        "match_filter": yt_dlp.match_filter_func("!is_live"),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        if youtube_violates_size_limits(ctx, ydl, node, link, log):
            return True
        if ctx.config.dry_run:
            print(f"Would download YouTube video {link} to {path} [Youtube]")
            return True
        path.mkdir(parents=True, exist_ok=True)
        ydl.download([link])
    return True


def cached_youtube_size_violates_limit(
    ctx: SyncContext,
    node: Node,
    path: Path,
    log: logging.Logger = logger,
) -> bool:
    old_node = course_cache.get_old_node_for(ctx, node, log)
    if old_node is None or not old_node.is_handled or old_node.remote_size is None:
        return False
    node.remote_size = old_node.remote_size
    return known_remote_size_violates_limit(ctx, node, path, log)


def youtube_violates_size_limits(
    ctx: SyncContext,
    ydl: Any,
    node: Node,
    link: str,
    log: logging.Logger = logger,
) -> bool:
    """Whether yt-dlp's pre-download size estimate falls outside the size limits.

    Best-effort: videos without a reported size are not limited.
    """
    if not size_limits_configured(ctx):
        return False
    try:
        info: dict[str, int] = ydl.extract_info(link, download=False)
    except Exception:
        return False
    total_size = youtube_estimated_size(info)
    if total_size is None:
        return False
    node.remote_size = total_size
    violation = size_limit_violation(ctx, total_size)
    if violation is None:
        return False
    log.warning(
        "Skipping YouTube video %s because its estimated size (%s) %s",
        link,
        human_readable_size(total_size),
        violation,
    )
    return True


def youtube_estimated_size(info: Any) -> int | None:
    """Extract yt-dlp's size estimate from an info dict, if it reports one."""
    if not isinstance(info, dict):
        return None
    total_size = info.get("filesize") or info.get("filesize_approx")
    if total_size:
        return int(total_size)
    # Merged downloads (separate video+audio) carry sizes per requested format.
    formats = info.get("requested_formats")
    if formats:
        sizes = [f.get("filesize") or f.get("filesize_approx") for f in formats]
        if all(sizes):
            return int(sum(sizes))
    return None
