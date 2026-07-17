"""Resolve public VEIRA watch links through the Cellia metadata API."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import ssl
import urllib.parse
import xml.etree.ElementTree as ElementTree
from contextlib import closing
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import requests

from syncmymoodle import filters
from syncmymoodle.constants import (
    EMEDIA_API_URL,
    EMEDIA_LINK_RE,
    EMEDIA_URL,
    HTTP_TIMEOUT_SECONDS,
)
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import (
    HttpFailureKind,
    classify_http_failure,
    classify_request_failure,
    read_capped_body,
    record_service_failure,
    redact_url_secrets,
    request_following_safe_redirects,
    safe_error_message,
    same_origin,
)
from syncmymoodle.node import DownloadKind, Node, RemoteMarkerKind

logger = logging.getLogger(__name__)
INTERMEDIATE_CERTIFICATE = "certs/HARICA-GEANT-TLS-R1.pem"
REQUEST_HEADERS = {"Origin": EMEDIA_URL.rstrip("/"), "Referer": EMEDIA_URL}
MANIFEST_MAX_BYTES = 1024 * 1024
WOWZA_SESSION_TOKEN_RE = re.compile(r"_w\d+")
_API_FAILURE = object()


@dataclass(frozen=True)
class EmediaVideo:
    id: int
    title: str
    playlist_url: str


@dataclass(frozen=True)
class EmediaResolution:
    """A cached metadata lookup: found, authoritatively absent, or failed."""

    video: EmediaVideo | None
    failure: str | None = None


class _CelliaTLSAdapter(requests.adapters.HTTPAdapter):
    """Supply the public intermediate certificate omitted by Cellia's server."""

    def __init__(self) -> None:
        certificate = resources.files("syncmymoodle").joinpath(
            *Path(INTERMEDIATE_CERTIFICATE).parts
        )
        self.ssl_context = ssl.create_default_context(
            cafile=requests.certs.where()  # type: ignore[attr-defined]
        )
        self.ssl_context.load_verify_locations(
            cadata=certificate.read_text(encoding="ascii")
        )
        super().__init__()

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        pool_kwargs["ssl_context"] = self.ssl_context
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        proxy_kwargs["ssl_context"] = self.ssl_context
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def extract_video_id(link: str) -> int | None:
    match = EMEDIA_LINK_RE.fullmatch(link)
    return int(match.group("video_id")) if match is not None else None


def _records_by_id(payload: Any) -> dict[int, dict[str, Any]] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        return None
    records: dict[int, dict[str, Any]] = {}
    for item in payload["records"]:
        if not isinstance(item, dict):
            return None
        raw_id = item.get("id")
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            record_id = raw_id
        elif isinstance(raw_id, str) and raw_id.isdigit():
            record_id = int(raw_id)
        else:
            return None
        if record_id <= 0 or record_id in records:
            return None
        records[record_id] = item
    return records


def _parse_video(payload: Any, video_id: int) -> EmediaResolution:
    records = _records_by_id(payload)
    if records is None:
        return EmediaResolution(None, "VEIRA returned malformed metadata")
    record = records.get(video_id)
    if record is None:
        return EmediaResolution(None)
    title = record.get("title")
    playlist_url = record.get("wowza_url")
    if not isinstance(title, str) or not title.strip():
        return EmediaResolution(None, f"VEIRA video {video_id} has no valid title")
    if not isinstance(playlist_url, str):
        return EmediaResolution(
            None,
            f"VEIRA video {video_id} has no valid playlist URL",
        )
    try:
        parsed_url = urllib.parse.urlsplit(playlist_url)
    except ValueError:
        return EmediaResolution(
            None,
            f"VEIRA video {video_id} has no valid playlist URL",
        )
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or not parsed_url.path.casefold().endswith(".m3u8")
    ):
        return EmediaResolution(
            None,
            f"VEIRA video {video_id} has an unsafe or invalid playlist URL",
        )
    return EmediaResolution(EmediaVideo(video_id, title.strip(), playlist_url))


def _api_session(ctx: SyncContext) -> requests.Session:
    if ctx.emedia_api_session is None:
        ctx.emedia_api_session = requests.Session()
        ctx.emedia_api_session.mount(EMEDIA_API_URL, _CelliaTLSAdapter())
    return ctx.emedia_api_session


def _record_api_failure(
    ctx: SyncContext,
    kind: HttpFailureKind,
    reason: str,
    log: logging.Logger,
) -> None:
    record_service_failure(
        ctx.service_outages,
        EMEDIA_API_URL,
        "emedia Medizin metadata API",
        kind,
        reason,
        log,
    )


def _fetch_video_payload(
    ctx: SyncContext,
    video_id: int,
    log: logging.Logger,
) -> Any:
    if ctx.service_outages.should_skip(EMEDIA_API_URL):
        return _API_FAILURE
    try:
        response = _api_session(ctx).post(
            EMEDIA_API_URL,
            json={"id": video_id},
            headers=REQUEST_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        _record_api_failure(
            ctx,
            classify_request_failure(error),
            f"video {video_id} request failed: {safe_error_message(error)}",
            log,
        )
        return _API_FAILURE

    with closing(response):
        failure_kind = classify_http_failure(response.status_code)
        if failure_kind is not None:
            reason = f"video {video_id} returned HTTP {response.status_code}"
            _record_api_failure(ctx, failure_kind, reason, log)
            if failure_kind is HttpFailureKind.RESOURCE:
                log.warning("Could not resolve VEIRA %s", reason)
            return _API_FAILURE
        try:
            payload = response.json()
        except ValueError as error:
            _record_api_failure(
                ctx,
                HttpFailureKind.TRANSIENT,
                f"video {video_id} returned invalid JSON: {safe_error_message(error)}",
                log,
            )
            return _API_FAILURE

    ctx.service_outages.record_available(EMEDIA_API_URL)
    return payload


def resolve_video(
    ctx: SyncContext,
    video_id: int,
    log: logging.Logger = logger,
) -> EmediaResolution:
    if video_id in ctx.emedia_video_cache:
        return ctx.emedia_video_cache[video_id]

    payload = _fetch_video_payload(ctx, video_id, log)
    if payload is _API_FAILURE:
        resolution = EmediaResolution(
            None,
            f"VEIRA metadata lookup failed for video {video_id}",
        )
        ctx.emedia_video_cache[video_id] = resolution
        return resolution

    resolution = _parse_video(payload, video_id)
    if resolution.failure is not None:
        log.warning("%s", resolution.failure)
    elif resolution.video is None:
        log.warning("VEIRA returned no usable metadata for video %s", video_id)
    ctx.emedia_video_cache[video_id] = resolution
    return resolution


def manifest_revision_marker(playlist_url: str, manifest: bytes) -> str | None:
    """Build a stable marker from a Wowza DASH manifest's media metadata."""
    try:
        root = ElementTree.fromstring(manifest)
    except ElementTree.ParseError:
        return None
    if root.tag.rsplit("}", 1)[-1] != "MPD":
        return None

    rows: list[tuple[str, tuple[tuple[str, str], ...], str]] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "Location":
            continue
        attributes = tuple(
            sorted(
                (name, WOWZA_SESSION_TOKEN_RE.sub("_wSESSION", value))
                for name, value in element.attrib.items()
                if name.rsplit("}", 1)[-1] != "publishTime"
            )
        )
        text = WOWZA_SESSION_TOKEN_RE.sub("_wSESSION", (element.text or "").strip())
        rows.append((element.tag, attributes, text))

    canonical = json.dumps(
        [playlist_url, rows],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _revision_marker(
    ctx: SyncContext,
    playlist_url: str,
    log: logging.Logger,
    course_id: Any = None,
) -> str | None:
    if playlist_url in ctx.emedia_revision_cache:
        return ctx.emedia_revision_cache[playlist_url]

    parsed_url = urllib.parse.urlsplit(playlist_url)
    manifest_path = parsed_url.path.rsplit("/", 1)[0] + "/manifest.mpd"
    manifest_url = urllib.parse.urlunsplit(
        parsed_url._replace(path=manifest_path, fragment="")
    )
    marker = None
    try:
        response = request_following_safe_redirects(
            _api_session(ctx),
            "GET",
            manifest_url,
            lambda url: (
                same_origin(url, playlist_url)
                and filters.require_url_allowed(
                    ctx,
                    url,
                    "emedia revision manifest",
                    course_id=course_id,
                )
            ),
            headers=REQUEST_HEADERS,
            stream=True,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        with closing(response):
            if 200 <= response.status_code < 300:
                body = read_capped_body(response, MANIFEST_MAX_BYTES)
                if body is not None:
                    marker = manifest_revision_marker(playlist_url, body)
    except requests.RequestException as error:
        log.warning(
            "Could not read VEIRA revision metadata from %s: %s",
            redact_url_secrets(manifest_url),
            safe_error_message(error),
        )

    if marker is None:
        log.warning(
            "VEIRA provided no usable revision metadata for %s; updates at the "
            "same URL cannot be detected",
            redact_url_secrets(playlist_url),
        )
    ctx.emedia_revision_cache[playlist_url] = marker
    return marker


def _output_suffix(ctx: SyncContext, log: logging.Logger) -> str:
    if ctx.emedia_output_suffix is None:
        ctx.emedia_output_suffix = ".mp4" if shutil.which("ffmpeg") else ".ts"
        if ctx.emedia_output_suffix == ".ts":
            log.warning(
                "FFmpeg is unavailable; saving VEIRA videos as MPEG-TS (.ts) "
                "instead of MP4"
            )
    return ctx.emedia_output_suffix


def add_video_node(
    ctx: SyncContext,
    parent_node: Node,
    link: str,
    module_title: Any = None,
    log: logging.Logger = logger,
    *,
    course_id: Any = None,
) -> bool:
    """Add a VEIRA node and report whether metadata resolution completed."""
    video_id = extract_video_id(link)
    if video_id is None:
        return True
    resolution = resolve_video(ctx, video_id, log)
    if resolution.failure is not None:
        return False
    video = resolution.video
    if video is None or filters.should_skip_url(
        ctx,
        video.playlist_url,
        "emedia video URL",
        course_id=course_id,
    ):
        return True
    marker = _revision_marker(ctx, video.playlist_url, log, course_id)
    output_suffix = _output_suffix(ctx, log)
    title = str(module_title or video.title)
    current_suffix = Path(title).suffix
    if current_suffix.casefold() in {".mp4", ".ts"}:
        title = title[: -len(current_suffix)]
    title += output_suffix
    parent_node.add_download_child(
        title,
        video.id,
        "Emedia",
        url=video.playlist_url,
        download_headers=REQUEST_HEADERS,
        etag=marker,
        etag_kind=RemoteMarkerKind.OPAQUE if marker is not None else None,
        download_kind=DownloadKind.EMEDIA,
    )
    return True
