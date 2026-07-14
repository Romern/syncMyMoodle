import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, cast

import requests

from syncmymoodle.constants import (
    CHECKSUM_LENGTHS_BY_ALGO,
    HTTP_TIMEOUT_SECONDS,
    MOODLE_URL,
    OPENCAST_EPISODE_URL_RE,
    OPENCAST_URL,
    RWTH_MOODLE_STATUS_URL,
)
from syncmymoodle.context import BrowserSessionUnavailable, SyncContext
from syncmymoodle.http_utils import (
    HttpFailureKind,
    classify_http_failure,
    parse_html,
    record_service_failure,
    redact_url_secrets,
    safe_request_error,
)
from syncmymoodle.node import Node, RemoteMarkerKind

logger = logging.getLogger(__name__)

OPENCAST_LTI_URL = f"{OPENCAST_URL}/lti"
OPENCAST_SEARCH_URL = f"{OPENCAST_URL}/search/episode.json"


@dataclass(frozen=True)
class OpencastTrack:
    url: str
    checksum_type: str | None = None
    checksum: str | None = None
    size: int | None = None
    duration: int | None = None

    @property
    def remote_marker(self) -> str | None:
        # The course cache stores remote version markers in Node.etag. For
        # Opencast, the episode API exposes a real content checksum for the
        # selected mp4 track, which is a better skip marker than a later GET
        # response ETag.
        return self.checksum

    @property
    def remote_marker_kind(self) -> RemoteMarkerKind | None:
        return RemoteMarkerKind.CONTENT_HASH if self.checksum else None


def add_track_node(
    parent_node: Node,
    name: str,
    episode_id: Any,
    track: OpencastTrack,
) -> Node | None:
    """Add a downloadable Opencast track with its remote metadata."""
    return parent_node.add_child(
        name,
        episode_id,
        "Opencast",
        url=track.url,
        etag=track.remote_marker,
        etag_kind=track.remote_marker_kind,
        remote_size=track.size,
    )


def log_backend_issue(
    ctx: SyncContext,
    reason: str,
    log: logging.Logger = logger,
) -> None:
    record_service_failure(
        ctx.service_outages,
        OPENCAST_URL,
        "Opencast",
        HttpFailureKind.TRANSIENT,
        reason,
        log,
        f"Check the RWTH ITC status page: {RWTH_MOODLE_STATUS_URL}",
    )


def _record_http_failure(
    ctx: SyncContext,
    status_code: int,
    context: str,
    log: logging.Logger,
) -> None:
    failure_kind = classify_http_failure(status_code)
    assert failure_kind is not None
    record_service_failure(
        ctx.service_outages,
        OPENCAST_URL,
        "Opencast",
        failure_kind,
        f"{context} returned HTTP {status_code}",
        log,
        f"Check the RWTH ITC status page: {RWTH_MOODLE_STATUS_URL}",
    )
    if failure_kind is HttpFailureKind.TRANSIENT:
        return
    log.warning("Opencast: %s returned HTTP %s", context, status_code)


def extract_episode_id(url: Any) -> str | None:
    if not url:
        return None

    url = str(url).replace("&amp;", "&")
    parsed = urllib.parse.urlparse(url)
    episode_ids = urllib.parse.parse_qs(parsed.query).get("episodeid", [])
    if episode_ids and episode_ids[0]:
        return str(episode_ids[0])

    match = OPENCAST_EPISODE_URL_RE.match(url)
    if match:
        return match.group(1)

    return None


def extract_lti_form_data(soup: Any) -> dict[str, Any]:
    return {
        input_tag["name"]: input_tag.get("value", "")
        for input_tag in soup.find_all("input")
        if input_tag.get("name")
    }


def submit_lti_form(
    ctx: SyncContext,
    engage_data: dict[str, Any],
    context: str,
    log: logging.Logger = logger,
    *,
    endpoint: str = OPENCAST_LTI_URL,
) -> bool:
    if ctx.service_outages.should_skip(OPENCAST_URL):
        return False
    if not engage_data:
        log.warning("Opencast: missing LTI form fields for %s", context)
        return False

    try:
        response = ctx.require_session().post(
            endpoint,
            data=engage_data,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        log_backend_issue(
            ctx,
            f"failed to submit LTI form for {context}: {safe_request_error(error)}",
            log,
        )
        return False

    if not (200 <= response.status_code < 300):
        _record_http_failure(
            ctx,
            response.status_code,
            f"LTI form for {context}",
            log,
        )
        return False

    ctx.service_outages.record_available(OPENCAST_URL)
    return True


def fetch_lti_form_data(
    ctx: SyncContext,
    url: str,
    context: str,
    log: logging.Logger = logger,
) -> dict[str, Any] | None:
    if ctx.service_outages.should_skip(OPENCAST_URL):
        return None
    try:
        response = ctx.require_browser_session().get(
            url,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        log.warning(
            "Opencast: failed to fetch LTI form for %s: %s",
            context,
            safe_request_error(error),
        )
        return None

    if not (200 <= response.status_code < 300):
        log.warning(
            "Opencast: LTI form returned status %s for %s",
            response.status_code,
            context,
        )
        return None

    soup = parse_html(response.text)
    engage_data = extract_lti_form_data(soup)
    if not engage_data:
        log.info("Opencast: no LTI form fields found for %s", context)
        return None

    return engage_data


def authenticate_episode(
    ctx: SyncContext,
    course_id: Any,
    episode_id: str,
    log: logging.Logger = logger,
) -> bool:
    if ctx.service_outages.should_skip(OPENCAST_URL):
        return False
    try:
        ctx.require_browser_session()
    except BrowserSessionUnavailable as error:
        if not ctx.browser_bootstrap_error_logged:
            log.warning("Opencast: %s", error)
            ctx.browser_bootstrap_error_logged = True
        return False
    if not ctx.browser_session_key:
        log.warning("Opencast: cannot launch episode without Moodle sesskey")
        return False

    cache_key = (course_id, episode_id)
    if cache_key in ctx.opencast_episode_auth_cache:
        return True

    params = urllib.parse.urlencode(
        {
            "courseid": course_id,
            "episodeid": episode_id,
            "sesskey": ctx.browser_session_key,
            "ocinstanceid": 1,
        }
    )
    info_url = f"{MOODLE_URL}filter/opencast/ltilaunch.php?{params}"
    context = f"episode {episode_id} in course {course_id}"
    engage_data = fetch_lti_form_data(ctx, info_url, context, log)
    if engage_data is None:
        return False
    if not submit_lti_form(ctx, engage_data, context, log):
        return False
    ctx.opencast_episode_auth_cache.add(cache_key)
    return True


def fetch_result_list(
    ctx: SyncContext,
    url: str,
    context: str,
    log: logging.Logger = logger,
) -> list[Any] | None:
    if ctx.service_outages.should_skip(OPENCAST_URL):
        return None
    try:
        response = ctx.require_session().get(url, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as error:
        log_backend_issue(
            ctx,
            f"failed to fetch {context} from {redact_url_secrets(url)}: "
            f"{safe_request_error(error)}",
            log,
        )
        return None

    if not (200 <= response.status_code < 300):
        _record_http_failure(
            ctx,
            response.status_code,
            f"{context} from {redact_url_secrets(url)}",
            log,
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        log_backend_issue(
            ctx,
            f"{context} from {redact_url_secrets(url)} returned invalid JSON",
            log,
        )
        return None

    if not isinstance(payload, dict):
        log_backend_issue(
            ctx,
            f"{context} returned {type(payload).__name__} instead of a JSON object",
            log,
        )
        return None

    if payload.get("error") or payload.get("errorcode"):
        ctx.service_outages.record_available(OPENCAST_URL)
        log.error(
            "Opencast: %s returned an error%s",
            context,
            f" ({payload.get('errorcode')})" if payload.get("errorcode") else "",
        )
        return None

    result = payload.get("result")
    if not isinstance(result, list):
        log_backend_issue(ctx, f"{context} response did not contain a result list", log)
        return None
    ctx.service_outages.record_available(OPENCAST_URL)
    if not result:
        log.warning("Opencast: empty result list for %s", context)
        return []
    return result


def resolution_width(resolution: Any) -> int:
    match = re.match(r"(\d+)\s*x\s*\d+", str(resolution or ""))
    if not match:
        return 0
    return int(match.group(1))


def optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def infer_checksum_type(checksum: str) -> str | None:
    for checksum_type, expected_length in CHECKSUM_LENGTHS_BY_ALGO.items():
        if len(checksum) == expected_length:
            return checksum_type
    return None


def extract_checksum(track: dict[str, Any]) -> tuple[str | None, str | None]:
    checksum_data = track.get("checksum")
    checksum_type: str | None = None
    checksum_value: str | None = None

    if isinstance(checksum_data, dict):
        raw_type = checksum_data.get("type")
        if isinstance(raw_type, str):
            checksum_type = raw_type.strip().lower()
        for key in ("$", "value", "#text"):
            raw_value = checksum_data.get(key)
            if isinstance(raw_value, str) and raw_value.strip():
                checksum_value = raw_value.strip()
                break
    elif isinstance(checksum_data, str):
        checksum_value = checksum_data.strip()

    if not checksum_value:
        return None, None

    checksum = checksum_value.lower()
    checksum_type = checksum_type or infer_checksum_type(checksum)
    expected_length = (
        CHECKSUM_LENGTHS_BY_ALGO.get(checksum_type) if checksum_type else None
    )
    if expected_length is None:
        return None, None
    if len(checksum) != expected_length:
        return None, None
    if not re.fullmatch(r"[0-9a-f]+", checksum):
        return None, None
    return checksum_type, checksum


def opencast_track_from_api(track: dict[str, Any]) -> OpencastTrack | None:
    video = track.get("video")
    url = track.get("url")
    if (
        not isinstance(url, str)
        or not url
        or track.get("mimetype") != "video/mp4"
        or "transport" in track
        or not isinstance(video, dict)
    ):
        return None

    checksum_type, checksum = extract_checksum(track)
    return OpencastTrack(
        url=url,
        checksum_type=checksum_type,
        checksum=checksum,
        size=optional_int(track.get("size")),
        duration=optional_int(track.get("duration")),
    )


def resolve_track_from_episode(  # noqa: C901 - legacy resolver awaiting decomposition
    ctx: SyncContext,
    episode_id: str,
    log: logging.Logger = logger,
) -> OpencastTrack | None:
    if episode_id in ctx.opencast_track_cache:
        return ctx.opencast_track_cache[episode_id]

    episode_url = f"{OPENCAST_SEARCH_URL}?id={episode_id}"
    tracks: list[tuple[int, OpencastTrack]] = []
    entries = fetch_result_list(ctx, episode_url, f"episode {episode_id}", log)
    if entries is None:
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mediapackage = entry.get("mediapackage")
        media = mediapackage.get("media") if isinstance(mediapackage, dict) else None
        track_data = media.get("track") if isinstance(media, dict) else None
        if isinstance(track_data, dict):
            track_data = [track_data]
        if not isinstance(track_data, list):
            continue
        for track in track_data:
            if not isinstance(track, dict):
                continue
            opencast_track = opencast_track_from_api(track)
            if opencast_track is None:
                continue
            video = cast(dict[str, Any], track["video"])
            tracks.append((resolution_width(video.get("resolution")), opencast_track))

    if not tracks:
        log.warning("Opencast: no downloadable mp4 track found for %s", episode_id)
        return None

    # Prefer the highest resolution plain HTTPS mp4 track.
    selected_track = max(tracks, key=lambda track: track[0])[1]
    ctx.opencast_track_cache[episode_id] = selected_track
    return selected_track
