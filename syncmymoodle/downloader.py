import hashlib
import logging
import math
import os
import re
import urllib.parse
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import requests
import yt_dlp

from syncmymoodle import course_cache, filters, links, opencast, pathing, quiz, storage
from syncmymoodle.constants import (
    DEFAULT_BLOCK_SIZE,
    HASH_ALGOS_BY_LENGTH,
    HTTP_TIMEOUT_SECONDS,
)
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import (
    HTML_CONTENT_TYPES,
    HttpFailureKind,
    classify_http_failure,
    classify_request_failure,
    content_length,
    content_type_without_parameters,
    normalized_http_origin,
    record_service_failure,
    redact_url_secrets,
    request_following_safe_redirects,
    safe_request_error,
)
from syncmymoodle.node import DownloadKind, Node, RemoteMarkerKind
from syncmymoodle.outcomes import (
    FAILED_DOWNLOAD,
    PLANNED_DOWNLOAD,
    POLICY_SKIPPED_DOWNLOAD,
    SKIPPED_DOWNLOAD,
    UNCHANGED_DOWNLOAD,
    DownloadOutcome,
    completed_download,
)
from syncmymoodle.output import TransferProgress, format_size

logger = logging.getLogger(__name__)
CONTENT_RANGE_RE = re.compile(
    r"^bytes\s+(?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+|\*)$",
    re.IGNORECASE,
)
YOUTUBE_AUXILIARY_EXTENSIONS = frozenset(
    {
        ".ass",
        ".description",
        ".gif",
        ".jpeg",
        ".jpg",
        ".json",
        ".lrc",
        ".png",
        ".srt",
        ".vtt",
        ".webp",
        ".ytdl",
    }
)


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


class DownloadDecision(Enum):
    """Outcome of change detection before conflict policy is applied."""

    DOWNLOAD = "download"
    SKIP = "skip"
    POLICY_SKIP = "policy_skip"
    CONFLICT = "conflict"


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


@dataclass(frozen=True)
class PlannedTransfer:
    action: ConflictAction
    baseline: storage.FileSnapshot


class YtDlpLogger:
    """Route yt-dlp messages through the application's logging policy."""

    def __init__(self, log: logging.Logger) -> None:
        self.log = log

    def debug(self, message: str) -> None:
        if message.startswith("[debug] "):
            self.log.debug("%s", message.removeprefix("[debug] "))
        else:
            self.log.info("%s", message)

    def info(self, message: str) -> None:
        self.log.info("%s", message)

    def warning(self, message: str) -> None:
        self.log.warning("%s", message)

    def error(self, message: str) -> None:
        self.log.error("%s", message)


def update_yt_dlp_progress(progress: TransferProgress, data: dict[str, Any]) -> None:
    """Translate a yt-dlp progress hook payload into the shared progress display."""
    if data.get("status") not in {"downloading", "finished"}:
        return
    completed = data.get("downloaded_bytes")
    total = data.get("total_bytes") or data.get("total_bytes_estimate")
    if not isinstance(completed, (int, float)) or isinstance(completed, bool):
        return
    completed_int = max(0, int(completed))
    total_int = (
        max(0, int(total))
        if isinstance(total, (int, float)) and not isinstance(total, bool)
        else None
    )
    progress.update(completed_int, total_int)


def yt_dlp_output_options(
    log: logging.Logger,
    progress: TransferProgress,
) -> dict[str, Any]:
    return {
        "logger": YtDlpLogger(log),
        "noprogress": True,
        "progress_hooks": [lambda data: update_yt_dlp_progress(progress, data)],
    }


def classify_local_file(
    path: Path,
    marker: str | None,
    snapshot: storage.FileSnapshot | None = None,
) -> FileMatch:
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
    if snapshot is not None:
        digest = snapshot.digest_for(algo)
        if digest is None:
            return FileMatch.UNKNOWN
    else:
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


def request_node_url(
    ctx: SyncContext,
    node: Node,
    **kwargs: Any,
) -> Any:
    assert node.url is not None
    course_node = _course_node(node)
    course_id = course_node.id if course_node is not None else None
    return request_following_safe_redirects(
        ctx.require_session(),
        "GET",
        node.url,
        lambda url: filters.require_url_allowed(
            ctx,
            url,
            f"redirected {node.type} file",
            course_id=course_id,
        ),
        **kwargs,
    )


def _report_download_request_failure(
    ctx: SyncContext,
    origin: str | None,
    url: str,
    error: requests.RequestException,
    log: logging.Logger,
) -> None:
    reason = (
        f"download of {redact_url_secrets(url)} failed: {safe_request_error(error)}"
    )
    failure_kind = classify_request_failure(error)
    if origin:
        record_service_failure(
            ctx.service_outages,
            origin,
            f"Download origin {origin}",
            failure_kind,
            reason,
            log,
        )
    if failure_kind is HttpFailureKind.RESOURCE and not isinstance(
        error, filters.FilteredRequestError
    ):
        log.warning("Skipping download request: %s", reason)


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
            redact_url_secrets(node.url),
        )
        return False

    if not (200 <= response.status_code < 300):
        log.warning(
            "Skipping download of %s from %s because the server returned HTTP %s",
            downloadpath,
            redact_url_secrets(node.url),
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
                redact_url_secrets(node.url),
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
    download_origin = normalized_http_origin(node.url)
    if download_origin and ctx.service_outages.should_skip(download_origin):
        return False
    if node.download_kind is DownloadKind.OPENCAST:
        if ctx.config.dry_run:
            return False
        authorized, _ = authorize_opencast_download(ctx, node, log)
        if not authorized:
            return False

    headers: dict[str, str] = {"If-None-Match": old_etag}
    if node.download_headers:
        headers = {**headers, **node.download_headers}

    try:
        with closing(
            request_node_url(
                ctx,
                node,
                headers=headers,
                stream=True,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
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
    except requests.RequestException as error:
        log.warning(
            "Failed to validate cached ETag for %s: %s",
            redact_url_secrets(node.url),
            safe_request_error(error),
        )
    return False


def remote_unchanged(
    ctx: SyncContext,
    node: Node,
    old_node: Node,
    cached_timemodified: Any,
    log: logging.Logger = logger,
) -> bool:
    """Whether the remote file is provably unchanged since our last download."""
    if node.has_remote_marker_conflict:
        return False
    old_etag = getattr(old_node, "etag", None)
    node_etag = getattr(node, "etag", None)

    # Comparable current markers are authoritative. In particular, a changed
    # content hash must win over a timestamp whose resolution is only seconds.
    if node_etag and node.etag_kind is RemoteMarkerKind.CONTENT_HASH:
        return bool(
            old_etag == node_etag
            and old_node.etag_kind in (None, RemoteMarkerKind.CONTENT_HASH)
        )
    if (
        old_etag
        and node_etag
        and old_node.etag_kind is not None
        and node.etag_kind is not None
        and old_node.etag_kind is not node.etag_kind
    ):
        return False
    if (
        old_etag
        and node_etag
        and (
            old_node.etag_kind == node.etag_kind
            or old_node.etag_kind is None
            or node.etag_kind is None
        )
    ):
        return bool(node_etag == old_etag)

    if cached_timemodified is not None:
        return bool(node.timemodified == cached_timemodified)

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
    baseline: storage.FileSnapshot,
) -> LocalCopyState:
    """Classify the on-disk file when the remote may have changed."""
    remote_etag = getattr(node, "etag", None)
    remote_etag_kind = node.etag_kind
    if (
        remote_etag_kind == RemoteMarkerKind.CONTENT_HASH
        and classify_local_file(downloadpath, remote_etag, baseline) is FileMatch.MATCH
    ):
        return LocalCopyState.UP_TO_DATE

    verdict = classify_local_file(
        downloadpath,
        local_verification_marker(old_node),
        baseline,
    )
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

    return LocalCopyState.MODIFIED


def decide_download(
    ctx: SyncContext,
    node: Node,
    downloadpath: Path,
    log: logging.Logger = logger,
    *,
    baseline: storage.FileSnapshot | None = None,
) -> DownloadDecision:
    """Decide whether ``node`` must be (re)downloaded and whether the local copy
    is user-modified.
    """
    if not downloadpath.exists():
        return DownloadDecision.DOWNLOAD
    if not ctx.config.update_files:
        return DownloadDecision.POLICY_SKIP

    old_node = course_cache.get_old_node_for(ctx, node, log)
    if old_node is not None and not old_node.is_verified:
        old_node = None
    cached_timemodified = (
        getattr(old_node, "timemodified", None) if old_node is not None else None
    )

    baseline = baseline or storage.snapshot_file(downloadpath)
    verdict = assess_local_copy(
        node,
        downloadpath,
        old_node,
        cached_timemodified,
        baseline,
    )
    if verdict is LocalCopyState.UP_TO_DATE:
        return DownloadDecision.SKIP
    if old_node is not None and remote_unchanged(
        ctx, node, old_node, cached_timemodified, log
    ):
        return DownloadDecision.SKIP
    if verdict is LocalCopyState.MODIFIED:
        return DownloadDecision.CONFLICT
    return DownloadDecision.DOWNLOAD


def should_skip_before_decision(
    ctx: SyncContext, node: Node, downloadpath: Path
) -> DownloadOutcome | None:
    course_node = _course_node(node)
    course_id = course_node.id if course_node is not None else None
    if filters.should_skip_url(
        ctx,
        node.url,
        f"{node.type} file",
        course_id=course_id,
    ):
        return SKIPPED_DOWNLOAD
    extension = Path(node.name).suffix.removeprefix(".").casefold()
    excluded_extensions = {
        configured.removeprefix(".").casefold()
        for configured in ctx.config.exclude_filetypes
    }
    if extension and extension in excluded_extensions:
        ctx.record_filtered(
            "filters.exclude_filetypes",
            "file",
            str(downloadpath),
            f"extension {extension!r} is excluded",
        )
        return SKIPPED_DOWNLOAD
    pattern = next(
        (
            pattern
            for pattern in ctx.config.exclude_files
            if fnmatchcase(node.name, pattern)
        ),
        None,
    )
    if pattern is not None:
        ctx.record_filtered(
            "filters.exclude_files",
            "file",
            str(downloadpath),
            f"matches {pattern!r}",
        )
        return SKIPPED_DOWNLOAD
    if downloadpath in ctx.downloaded_paths:
        return UNCHANGED_DOWNLOAD
    return None


def conflict_action(
    ctx: SyncContext,
    downloadpath: Path,
    log: logging.Logger = logger,
) -> ConflictAction:
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


def size_limit_violation(ctx: SyncContext, size: int) -> tuple[str, str] | None:
    """Which of filters.max_file_size/min_file_size ``size`` violates, if any."""
    max_size = ctx.config.max_file_size
    if max_size and size > max_size:
        return (
            "filters.max_file_size",
            f"exceeds the configured limit ({format_size(max_size)})",
        )
    min_size = ctx.config.min_file_size
    if min_size and size < min_size:
        return (
            "filters.min_file_size",
            f"is below the configured limit ({format_size(min_size)})",
        )
    return None


def record_size_limit_filter(
    ctx: SyncContext,
    item: str,
    size: int,
    size_kind: str,
) -> bool:
    violation = size_limit_violation(ctx, size)
    if violation is None:
        return False
    config_key, reason = violation
    ctx.record_filtered(
        config_key,
        "file",
        item,
        f"{size_kind} ({format_size(size)}) {reason}",
    )
    return True


def known_remote_size_violates_limit(
    ctx: SyncContext,
    node: Node,
    downloadpath: Path,
) -> bool:
    if not size_limits_configured(ctx) or node.remote_size is None:
        return False
    return record_size_limit_filter(
        ctx,
        str(downloadpath),
        node.remote_size,
        "known size",
    )


def download_violates_size_limits(
    ctx: SyncContext,
    node: Node,
    response: Any,
    resume_size: int,
    downloadpath: Path,
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
    return record_size_limit_filter(ctx, str(downloadpath), total_size, "size")


def _course_node(node: Node) -> Node | None:
    try:
        return course_cache.get_course_node(node)
    except Exception:
        return None


def stable_download_decision(
    ctx: SyncContext,
    node: Node,
    downloadpath: Path,
    log: logging.Logger,
) -> tuple[DownloadDecision, storage.FileSnapshot]:
    """Classify a target against a content baseline that stayed unchanged."""
    for _ in range(3):
        baseline = storage.snapshot_file(downloadpath)
        decision = decide_download(ctx, node, downloadpath, log, baseline=baseline)
        if baseline.metadata_still_matches(downloadpath):
            return decision, baseline

    # A continuously changing target cannot be classified reliably. Treat it
    # as a conflict so the configured policy fails closed.
    baseline = storage.snapshot_file(downloadpath)
    decision = (
        DownloadDecision.CONFLICT if baseline.exists else DownloadDecision.DOWNLOAD
    )
    return decision, baseline


def planned_download_action(
    ctx: SyncContext,
    node: Node,
    downloadpath: Path,
    log: logging.Logger = logger,
) -> PlannedTransfer | DownloadOutcome:
    """Return the transfer action or the outcome when no transfer is needed."""
    early_outcome = should_skip_before_decision(ctx, node, downloadpath)
    if early_outcome is not None:
        return early_outcome
    if node.download_kind is DownloadKind.OPENCAST:
        course_node = _course_node(node)
        course_id = course_node.id if course_node is not None else None
        if opencast.episode_metadata_is_stale(
            ctx,
            course_id,
            str(node.id or ""),
        ):
            if downloadpath.exists() and not ctx.config.update_files:
                return POLICY_SKIPPED_DOWNLOAD
            log.warning(
                "Skipping Opencast download %s because its metadata could not be "
                "refreshed",
                downloadpath,
            )
            return FAILED_DOWNLOAD
    if known_remote_size_violates_limit(ctx, node, downloadpath):
        return SKIPPED_DOWNLOAD

    decision, baseline = stable_download_decision(ctx, node, downloadpath, log)
    if decision is DownloadDecision.POLICY_SKIP:
        return POLICY_SKIPPED_DOWNLOAD
    if decision is DownloadDecision.SKIP:
        if (
            node.etag_kind is RemoteMarkerKind.CONTENT_HASH
            and classify_local_file(downloadpath, node.etag, baseline)
            is FileMatch.MATCH
        ):
            align_mtime_with_timemodified(node, downloadpath)
        return UNCHANGED_DOWNLOAD
    action = (
        conflict_action(ctx, downloadpath, log)
        if decision is DownloadDecision.CONFLICT
        else ConflictAction.DOWNLOAD
    )
    if action == ConflictAction.SKIP:
        return POLICY_SKIPPED_DOWNLOAD
    return PlannedTransfer(action, baseline)


def report_planned_download(
    ctx: SyncContext,
    target: str | Path,
    kind: str,
    *,
    verb: str = "Would download",
) -> DownloadOutcome:
    ctx.output.action(verb, target, kind, dry_run=True)
    return PLANNED_DOWNLOAD


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


def valid_resume_content_range(value: Any, resume_size: int) -> bool:
    if not isinstance(value, str):
        return False
    match = CONTENT_RANGE_RE.fullmatch(value.strip())
    if match is None:
        return False
    start = int(match.group("start"))
    end = int(match.group("end"))
    total = match.group("total")
    return start == resume_size and end >= start and (total == "*" or end < int(total))


def validate_resume_response(response: Any, transfer: TransferPlan) -> bool:
    if not transfer.resume_size:
        return True

    etag_header = response.headers.get("ETag")
    valid_resume = (
        response.status_code == 206
        and etag_header == transfer.partial_etag
        and valid_resume_content_range(
            response.headers.get("Content-Range"), transfer.resume_size
        )
    )
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
        redact_url_secrets(node.url),
    )
    return False


def write_response_body(
    ctx: SyncContext,
    node: Node,
    response: Any,
    transfer: TransferPlan,
    downloadpath: Path,
    content: Any,
    first_chunk: bytes,
) -> int:
    ctx.output.action("Downloading", downloadpath, node.type)
    total_size_in_bytes = content_length(response, transfer.resume_size)
    with ctx.output.transfer(
        downloadpath.name,
        total_size_in_bytes,
        transfer.resume_size,
    ) as progress:
        if transfer.resume_size:
            progress.update(transfer.resume_size, total_size_in_bytes)
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
                progress.advance(len(first_chunk))
                file.write(first_chunk)
            for data in content:
                progress.advance(len(data))
                file.write(data)
    return progress.transferred_bytes


def install_downloaded_file(
    downloadpath: Path,
    transfer: TransferPlan,
    planned: PlannedTransfer,
    target_change_policy: str,
    log: logging.Logger = logger,
) -> storage.InstallResult:
    result = storage.install_staged_file(
        transfer.tmp_path,
        downloadpath,
        baseline=planned.baseline,
        rename_local=planned.action is ConflictAction.RENAME_LOCAL,
        target_change_policy=target_change_policy,
        description="the updated file from Moodle",
        log=log,
    )
    if result is not storage.InstallResult.INSTALLED:
        transfer.discard_partial()
        return result
    transfer.etag_sidecar.unlink(missing_ok=True)
    return result


def noninstalled_download_outcome(
    result: storage.InstallResult,
    transferred_bytes: int,
) -> DownloadOutcome | None:
    if result is storage.InstallResult.INSTALLED:
        return None
    if result is storage.InstallResult.KEPT_LOCAL:
        return DownloadOutcome(
            unchanged=1,
            transferred_bytes=transferred_bytes,
            cache_verified=False,
        )
    return FAILED_DOWNLOAD


def record_download_metadata(
    node: Node,
    downloadpath: Path,
    etag_header: str | None,
    content_hash: str | None = None,
) -> None:
    node.content_hash = content_hash or storage.file_sha256(downloadpath)

    align_mtime_with_timemodified(node, downloadpath)

    if etag_header is not None and node.etag is None:
        node.etag = etag_header
        node.etag_kind = RemoteMarkerKind.OPAQUE


def process_download_response(
    ctx: SyncContext,
    node: Node,
    response: Any,
    transfer: TransferPlan | None,
    downloadpath: Path,
    planned: PlannedTransfer,
    log: logging.Logger,
) -> DownloadOutcome:
    etag_header = response.headers.get("ETag")

    if transfer is not None and not validate_resume_response(response, transfer):
        return FAILED_DOWNLOAD

    resume_size = transfer.resume_size if transfer is not None else 0
    if not download_response_is_usable(node, response, downloadpath, log):
        return FAILED_DOWNLOAD
    if download_violates_size_limits(ctx, node, response, resume_size, downloadpath):
        return SKIPPED_DOWNLOAD

    if ctx.config.dry_run:
        return report_planned_download(ctx, downloadpath, node.type)

    assert transfer is not None
    content = response.iter_content(DEFAULT_BLOCK_SIZE)
    first_chunk = next((chunk for chunk in content if chunk), b"")
    if not response_body_is_usable(node, first_chunk, downloadpath, log):
        return FAILED_DOWNLOAD

    existed = downloadpath.exists()
    transferred_bytes = write_response_body(
        ctx,
        node,
        response,
        transfer,
        downloadpath,
        content,
        first_chunk,
    )
    staged_hash = storage.file_sha256(transfer.tmp_path)
    local_hash = storage.file_sha256(downloadpath) if downloadpath.exists() else None
    if staged_hash is not None and staged_hash == local_hash:
        transfer.discard_partial()
        record_download_metadata(node, downloadpath, etag_header, staged_hash)
        ctx.downloaded_paths.add(downloadpath)
        return DownloadOutcome(
            unchanged=1,
            transferred_bytes=transferred_bytes,
        )
    install_result = install_downloaded_file(
        downloadpath,
        transfer,
        planned,
        ctx.config.conflict_handling,
        log,
    )
    install_outcome = noninstalled_download_outcome(
        install_result,
        transferred_bytes,
    )
    if install_outcome is not None:
        return install_outcome
    record_download_metadata(node, downloadpath, etag_header, staged_hash)
    ctx.downloaded_paths.add(downloadpath)
    return completed_download(existed=existed, transferred_bytes=transferred_bytes)


def _classify_download_response(
    ctx: SyncContext,
    node: Node,
    response: Any,
    request_origin: str | None,
    log: logging.Logger,
) -> HttpFailureKind | None:
    response_origin = normalized_http_origin(response.url or node.url) or request_origin
    if request_origin and response_origin != request_origin:
        ctx.service_outages.record_available(request_origin)
    failure_kind = classify_http_failure(response.status_code)
    if response_origin is None:
        return failure_kind
    if failure_kind is None:
        ctx.service_outages.record_available(response_origin)
    else:
        record_service_failure(
            ctx.service_outages,
            response_origin,
            f"Download origin {response_origin}",
            failure_kind,
            f"GET {redact_url_secrets(node.url)} returned HTTP {response.status_code}",
            log,
        )
    return failure_kind


def authorize_opencast_download(
    ctx: SyncContext,
    node: Node,
    log: logging.Logger,
) -> tuple[bool, Any]:
    course_node = _course_node(node)
    if course_node is None:
        log.warning("Cannot authorize Opencast download outside a course")
        return False, None
    episode_id = str(node.id or "")
    if not episode_id:
        log.warning("Cannot authorize Opencast download without an episode id")
        return False, course_node.id
    if not opencast.course_is_authorized(ctx, course_node.id):
        ctx.output.action("Authorizing", course_node.name, "Opencast")
    return (
        opencast.authorize_course_for_episode(
            ctx,
            course_node.id,
            episode_id,
            log,
        ),
        course_node.id,
    )


def download_file(
    ctx: SyncContext,
    node: Node,
    log: logging.Logger = logger,
) -> DownloadOutcome:
    """Download file with progress bar if it isn't already downloaded."""
    downloadpath = pathing.get_sanitized_node_path(
        node, Path(ctx.config.sync_directory)
    )

    if not node.url:
        return FAILED_DOWNLOAD
    download_origin = normalized_http_origin(node.url)
    if download_origin and ctx.service_outages.should_skip(download_origin):
        return FAILED_DOWNLOAD

    action_or_outcome = planned_download_action(ctx, node, downloadpath, log)
    if isinstance(action_or_outcome, DownloadOutcome):
        return action_or_outcome
    planned = action_or_outcome

    if ctx.config.dry_run and (
        node.download_kind is DownloadKind.OPENCAST or not size_limits_configured(ctx)
    ):
        return report_planned_download(ctx, downloadpath, node.type)

    opencast_course_id: Any = None
    if node.download_kind is DownloadKind.OPENCAST:
        authorized, opencast_course_id = authorize_opencast_download(ctx, node, log)
        if not authorized:
            return FAILED_DOWNLOAD

    transfer = None if ctx.config.dry_run else prepare_transfer_plan(node, downloadpath)
    headers = (
        dict(node.download_headers)
        if transfer is None and node.download_headers
        else transfer.headers
        if transfer is not None
        else {}
    )
    try:
        response = request_node_url(
            ctx,
            node,
            headers=headers,
            stream=True,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        _report_download_request_failure(
            ctx,
            download_origin,
            node.url,
            error,
            log,
        )
        return FAILED_DOWNLOAD

    with closing(response):
        failure_kind = _classify_download_response(
            ctx, node, response, download_origin, log
        )
        if failure_kind is HttpFailureKind.TRANSIENT:
            return FAILED_DOWNLOAD
        outcome = process_download_response(
            ctx,
            node,
            response,
            transfer,
            downloadpath,
            planned,
            log,
        )
        if (
            node.download_kind is DownloadKind.OPENCAST
            and not outcome.is_handled
            and (
                response.status_code in {401, 403, 404, 410}
                or content_type_without_parameters(response) in HTML_CONTENT_TYPES
            )
        ):
            opencast.invalidate_episode(
                ctx,
                opencast_course_id,
                str(node.id or ""),
            )
        return outcome


def download_all_files(
    ctx: SyncContext,
    log: logging.Logger = logger,
) -> None:
    ctx.require_session()
    ctx.require_moodle_account()
    if not ctx.root_node:
        raise Exception("You need to sync() first.")

    download_node_tree(ctx, ctx.root_node, log)


def download_leaf(
    ctx: SyncContext,
    node: Node,
    log: logging.Logger,
) -> DownloadOutcome:
    try:
        assert node.url is not None
        if node.download_kind is DownloadKind.YOUTUBE:
            return scan_and_download_youtube(ctx, node, log)
        if node.download_kind is DownloadKind.EMEDIA:
            return download_emedia_video(ctx, node, log)
        if node.download_kind is DownloadKind.QUIZ:
            return quiz.download_quiz(ctx, node, log)
        return download_file(ctx, node, log)
    except Exception:
        log.exception("Failed to download the module %s", node)
        if node.download_kind in {DownloadKind.YOUTUBE, DownloadKind.EMEDIA}:
            log.error(
                "This could be caused by an out-of-date yt-dlp version. Try "
                "upgrading yt-dlp through pip or your package manager."
            )
        return FAILED_DOWNLOAD


def download_node_tree(
    ctx: SyncContext,
    cur_node: Node,
    log: logging.Logger = logger,
) -> None:
    pending: list[Node] = []

    def collect(node: Node) -> None:
        if not node.children:
            if node.url and not node.is_handled:
                pending.append(node)
            return
        for child in node.children:
            collect(child)

    collect(cur_node)
    progress = ctx.output.sync_progress
    progress.begin_items(len(pending), dry_run=ctx.config.dry_run)
    for index, node in enumerate(pending, start=1):
        path = "/".join(part for part in node.get_path() if part)
        progress.start_item(index, f"{node.type}: {path or node.name}")
        outcome = download_leaf(ctx, node, log)
        ctx.stats.record_download(outcome)
        if outcome.is_handled:
            if outcome.cache_verified:
                node.mark_handled()
            else:
                node.mark_skipped()
        progress.finish_item(index)


def download_emedia_video(
    ctx: SyncContext,
    node: Node,
    log: logging.Logger = logger,
) -> DownloadOutcome:
    """Download the best single stream from a VEIRA HLS playlist."""
    if node.url is None:
        return FAILED_DOWNLOAD
    downloadpath = pathing.get_sanitized_node_path(
        node, Path(ctx.config.sync_directory)
    )
    action_or_outcome = planned_download_action(ctx, node, downloadpath, log)
    if isinstance(action_or_outcome, DownloadOutcome):
        return action_or_outcome
    planned = action_or_outcome
    if cached_yt_dlp_size_violates_limit(ctx, node, downloadpath, log):
        return SKIPPED_DOWNLOAD
    if ctx.config.dry_run and not size_limits_configured(ctx):
        return report_planned_download(ctx, downloadpath, "Emedia")

    existed = downloadpath.exists()
    suffix = downloadpath.suffix or ".mp4"
    stem = (
        downloadpath.name[: -len(suffix)] if downloadpath.suffix else downloadpath.name
    )
    temporary_path = pathing.with_windows_extended_length_prefix(
        downloadpath.parent / f".{stem}.smmpart{suffix}"
    )
    progress = ctx.output.transfer(downloadpath.name, node.remote_size)
    ydl_opts: dict[str, Any] = {
        "format": "best",
        "fragment_retries": 15,
        "http_headers": dict(node.download_headers or {}),
        "noplaylist": True,
        "nooverwrites": True,
        "outtmpl": os.fspath(temporary_path),
        "retries": 15,
        **yt_dlp_output_options(log, progress),
    }
    if suffix.casefold() == ".ts":
        ydl_opts["fixup"] = "never"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        if yt_dlp_violates_size_limits(ctx, ydl, node, node.url, "emedia video"):
            return SKIPPED_DOWNLOAD
        if ctx.config.dry_run:
            return report_planned_download(ctx, downloadpath, "Emedia")
        ctx.output.action("Downloading", downloadpath, "Emedia")
        downloadpath.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.unlink(missing_ok=True)
        with progress:
            result = ydl.download([node.url])
        if result not in (None, 0) or not temporary_path.is_file():
            log.warning("yt-dlp did not download VEIRA video %s", node.id)
            return FAILED_DOWNLOAD

    transfer = TransferPlan(
        temporary_path,
        temporary_path.with_name(temporary_path.name + ".etag"),
        {},
    )
    install_result = install_downloaded_file(
        downloadpath,
        transfer,
        planned,
        ctx.config.conflict_handling,
        log,
    )
    install_outcome = noninstalled_download_outcome(
        install_result,
        progress.transferred_bytes,
    )
    if install_outcome is not None:
        return install_outcome
    record_download_metadata(node, downloadpath, None)
    ctx.downloaded_paths.add(downloadpath)
    return completed_download(
        existed=existed,
        transferred_bytes=progress.transferred_bytes,
    )


def youtube_download_exists(path: Path, video_id: str | None) -> bool:
    if not video_id or not path.is_dir():
        return False
    completed_name = re.compile(rf"-{re.escape(video_id)}\.[^.]+$")
    return any(
        file.is_file()
        and file.suffix.casefold() not in YOUTUBE_AUXILIARY_EXTENSIONS
        and completed_name.search(file.name)
        for file in path.iterdir()
    )


def scan_and_download_youtube(
    ctx: SyncContext,
    node: Node,
    log: logging.Logger = logger,
) -> DownloadOutcome:
    """Download Youtube-Videos using yt_dlp."""
    if node.parent is None or node.url is None:
        return FAILED_DOWNLOAD
    path = pathing.get_sanitized_node_path(node.parent, Path(ctx.config.sync_directory))
    link = node.url
    course_node = _course_node(node)
    course_id = course_node.id if course_node is not None else None
    if filters.should_skip_url(
        ctx,
        link,
        "YouTube link",
        course_id=course_id,
    ):
        return SKIPPED_DOWNLOAD
    video_id = links.youtube_video_id_from_node(node)
    if youtube_download_exists(path, video_id):
        return UNCHANGED_DOWNLOAD
    if ctx.config.dry_run and not size_limits_configured(ctx):
        return report_planned_download(
            ctx,
            f"{link} to {path}",
            "Youtube",
            verb="Would download YouTube video",
        )
    if cached_yt_dlp_size_violates_limit(ctx, node, path, log):
        return SKIPPED_DOWNLOAD
    outtmpl = pathing.with_windows_extended_length_prefix(
        path / "%(title)s-%(id)s.%(ext)s",
        force=True,
    )
    progress = ctx.output.transfer(node.name or "YouTube video", node.remote_size)
    ydl_opts: dict[str, Any] = {
        "outtmpl": os.fspath(outtmpl),
        "ignoreerrors": True,
        "nooverwrites": True,
        "retries": 15,
        "match_filter": yt_dlp.match_filter_func("!is_live"),
        **yt_dlp_output_options(log, progress),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        if yt_dlp_violates_size_limits(ctx, ydl, node, link, "YouTube video"):
            return SKIPPED_DOWNLOAD
        if ctx.config.dry_run:
            return report_planned_download(
                ctx,
                f"{link} to {path}",
                "Youtube",
                verb="Would download YouTube video",
            )
        ctx.output.action("Downloading YouTube video", f"{link} to {path}", "Youtube")
        path.mkdir(parents=True, exist_ok=True)
        with progress:
            result = ydl.download([link])
    if result not in (None, 0):
        return FAILED_DOWNLOAD
    if not youtube_download_exists(path, video_id):
        log.warning(
            "yt-dlp did not download YouTube video %s; it may have been filtered",
            video_id or link,
        )
        return FAILED_DOWNLOAD
    return completed_download(
        existed=False,
        transferred_bytes=progress.transferred_bytes,
    )


def cached_yt_dlp_size_violates_limit(
    ctx: SyncContext,
    node: Node,
    path: Path,
    log: logging.Logger = logger,
) -> bool:
    old_node = course_cache.get_old_node_for(ctx, node, log)
    if old_node is None or not old_node.is_verified or old_node.remote_size is None:
        return False
    node.remote_size = old_node.remote_size
    return known_remote_size_violates_limit(ctx, node, path)


def yt_dlp_violates_size_limits(
    ctx: SyncContext,
    ydl: Any,
    node: Node,
    link: str,
    description: str,
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
    total_size = yt_dlp_estimated_size(info)
    if total_size is None:
        return False
    node.remote_size = total_size
    return record_size_limit_filter(
        ctx,
        f"{description} {redact_url_secrets(link)}",
        total_size,
        "estimated size",
    )


def yt_dlp_estimated_size(info: Any) -> int | None:
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
    duration = info.get("duration")
    total_bitrate = info.get("tbr")
    if (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and math.isfinite(duration)
        and duration > 0
        and isinstance(total_bitrate, (int, float))
        and not isinstance(total_bitrate, bool)
        and math.isfinite(total_bitrate)
        and total_bitrate > 0
    ):
        # yt-dlp reports total bitrate in kilobits per second.
        return round(duration * total_bitrate * 1000 / 8)
    return None
