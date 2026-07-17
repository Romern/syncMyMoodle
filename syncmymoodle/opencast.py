import hashlib
import logging
import re
import urllib.parse
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator, cast

import requests

from syncmymoodle import filters
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
    RequestPolicyError,
    classify_http_failure,
    normalized_http_origin,
    parse_html,
    record_service_failure,
    redact_url_secrets,
    request_following_safe_redirects,
    safe_request_error,
    same_origin,
)
from syncmymoodle.node import DownloadKind, Node, RemoteMarkerKind

logger = logging.getLogger(__name__)

OPENCAST_LTI_URL = f"{OPENCAST_URL}/lti"
OPENCAST_SEARCH_URL = f"{OPENCAST_URL}/search/episode.json"
OPENCAST_SERIES_PAGE_SIZE = 100
OPENCAST_EPISODES_CACHE_FORMAT = "syncmymoodle.opencast-episodes.v1"


@dataclass(frozen=True)
class OpencastTrack:
    url: str
    checksum_type: str | None = None
    checksum: str | None = None
    size: int | None = None
    duration: int | None = None
    flavor_type: str | None = None

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


@dataclass(frozen=True)
class OpencastEpisode:
    """Last known tracks plus the remote scope used to refresh them."""

    tracks: tuple[OpencastTrack, ...]
    series_id: str | None = None


class OpencastMetadataState(Enum):
    """Authoritative state for this run; no entry means not validated yet."""

    # FRESH also represents an authoritative response with no usable track.
    FRESH = "fresh"
    # STALE metadata may preserve local files but must not drive a transfer.
    STALE = "stale"


def _track_node_name(
    name: Any,
    track: OpencastTrack,
) -> str:
    flavor_label = track.flavor_type or (
        f"video-{hashlib.sha256(track.url.encode()).hexdigest()[:8]}"
    )
    base_name = (
        str(name) if name else urllib.parse.urlparse(track.url).path.rsplit("/", 1)[-1]
    )
    base_name = base_name or flavor_label
    stem = base_name[:-4] if base_name.casefold().endswith(".mp4") else base_name
    return f"{stem} ({flavor_label}).mp4"


def add_episode_nodes(
    ctx: SyncContext,
    parent_node: Node,
    name: Any,
    episode_id: str,
    log: logging.Logger = logger,
    *,
    course_id: Any = None,
) -> None:
    tracks = resolve_tracks_from_episode(
        ctx,
        episode_id,
        log,
        course_id=course_id,
    )
    if tracks is None:
        return

    for track in tracks:
        if filters.should_skip_url(
            ctx,
            track.url,
            "Opencast video URL",
            course_id=course_id,
        ):
            continue
        parent_node.add_download_child(
            _track_node_name(name, track),
            episode_id,
            "Opencast",
            url=track.url,
            etag=track.remote_marker,
            etag_kind=track.remote_marker_kind,
            remote_size=track.size,
            download_kind=DownloadKind.OPENCAST,
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


def lti_endpoint_allowed(endpoint: Any) -> bool:
    """Return whether an LTI payload may be posted to the Opencast launch URL."""
    if not isinstance(endpoint, str) or endpoint != endpoint.strip():
        return False
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        expected = urllib.parse.urlsplit(OPENCAST_LTI_URL)
    except ValueError:
        return False
    return (
        parsed.username is None
        and parsed.password is None
        and same_origin(endpoint, OPENCAST_URL)
        and parsed.path in {expected.path, f"{expected.path}/"}
        and not parsed.query
        and not parsed.fragment
    )


def opencast_redirect_url_allowed(url: Any) -> bool:
    """Return whether an authenticated Opencast redirect stays on its origin."""
    if not isinstance(url, str) or url != url.strip():
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.username is None
        and parsed.password is None
        and same_origin(url, OPENCAST_URL)
    )


def submit_lti_form(
    ctx: SyncContext,
    engage_data: dict[str, Any],
    context: str,
    log: logging.Logger = logger,
    *,
    endpoint: str = OPENCAST_LTI_URL,
    course_id: Any = None,
) -> bool:
    if ctx.service_outages.should_skip(OPENCAST_URL):
        return False
    if not engage_data:
        log.warning("Opencast: missing LTI form fields for %s", context)
        return False
    if not lti_endpoint_allowed(endpoint):
        log.warning("Opencast: refusing unexpected LTI endpoint for %s", context)
        return False

    try:
        response = request_following_safe_redirects(
            ctx.require_session(),
            "POST",
            endpoint,
            opencast_redirect_url_allowed,
            data=engage_data,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except RequestPolicyError as error:
        ctx.service_outages.record_available(OPENCAST_URL)
        log.warning(
            "Opencast: refusing unsafe LTI redirect for %s: %s",
            context,
            safe_request_error(error),
        )
        return False
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
    record_course_authorized(ctx, endpoint, course_id)
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


def _course_id_key(course_id: Any) -> str | None:
    if course_id is None or isinstance(course_id, bool):
        return None
    try:
        parsed = int(course_id)
    except (TypeError, ValueError):
        return None
    return str(parsed) if parsed > 0 else None


def _course_auth_key(endpoint: str, course_id: Any) -> tuple[str, str] | None:
    origin = normalized_http_origin(endpoint)
    course_key = _course_id_key(course_id)
    return (
        (origin, course_key) if origin is not None and course_key is not None else None
    )


def record_course_authorized(
    ctx: SyncContext,
    endpoint: str,
    course_id: Any,
) -> None:
    cache_key = _course_auth_key(endpoint, course_id)
    if cache_key is not None:
        ctx.opencast_course_auth_cache.add(cache_key)


def course_is_authorized(
    ctx: SyncContext,
    course_id: Any,
    endpoint: str = OPENCAST_URL,
) -> bool:
    cache_key = _course_auth_key(endpoint, course_id)
    return cache_key is not None and cache_key in ctx.opencast_course_auth_cache


def authorize_course_for_episode(
    ctx: SyncContext,
    course_id: Any,
    episode_id: str,
    log: logging.Logger = logger,
) -> bool:
    if ctx.service_outages.should_skip(OPENCAST_URL):
        return False
    if course_is_authorized(ctx, course_id):
        return True
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
    if not submit_lti_form(ctx, engage_data, context, log, course_id=course_id):
        return False
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


def _validated_checksum(
    checksum_type: Any,
    checksum_value: Any,
) -> tuple[str | None, str | None]:
    if not isinstance(checksum_value, str) or not checksum_value.strip():
        return None, None
    checksum = checksum_value.strip().lower()
    parsed_type = (
        checksum_type.strip().lower()
        if isinstance(checksum_type, str) and checksum_type.strip()
        else infer_checksum_type(checksum)
    )
    expected_length = CHECKSUM_LENGTHS_BY_ALGO.get(parsed_type) if parsed_type else None
    if (
        expected_length is None
        or len(checksum) != expected_length
        or re.fullmatch(r"[0-9a-f]+", checksum) is None
    ):
        return None, None
    return parsed_type, checksum


def track_cache_data(track: OpencastTrack) -> dict[str, Any]:
    return {
        "url": track.url,
        **(
            {
                "checksum_type": track.checksum_type,
                "checksum": track.checksum,
            }
            if track.checksum_type is not None and track.checksum is not None
            else {}
        ),
        **({"size": track.size} if track.size is not None else {}),
        **({"duration": track.duration} if track.duration is not None else {}),
        **({"flavor_type": track.flavor_type} if track.flavor_type is not None else {}),
    }


def track_from_cache_data(value: Any) -> OpencastTrack | None:
    if not isinstance(value, dict):
        return None
    url = value.get("url")
    if not isinstance(url, str) or not url:
        return None

    checksum = value.get("checksum")
    checksum_type = value.get("checksum_type")
    if checksum is None and checksum_type is None:
        parsed_checksum = None
        parsed_checksum_type = None
    else:
        parsed_checksum_type, parsed_checksum = _validated_checksum(
            checksum_type,
            checksum,
        )
        if parsed_checksum is None:
            return None

    flavor = value.get("flavor_type")
    if flavor is not None and (not isinstance(flavor, str) or not flavor):
        return None
    return OpencastTrack(
        url=url,
        checksum_type=parsed_checksum_type,
        checksum=parsed_checksum,
        size=optional_int(value.get("size")),
        duration=optional_int(value.get("duration")),
        flavor_type=flavor,
    )


def episode_cache_data(episode: OpencastEpisode) -> dict[str, Any]:
    return {
        "tracks": [track_cache_data(track) for track in episode.tracks],
        **({"series_id": episode.series_id} if episode.series_id is not None else {}),
    }


def episode_from_cache_data(value: Any) -> OpencastEpisode | None:
    if not isinstance(value, dict) or not isinstance(value.get("tracks"), list):
        return None
    raw_tracks = value["tracks"]
    tracks = tuple(
        track
        for raw_track in raw_tracks
        if (track := track_from_cache_data(raw_track)) is not None
    )
    if not tracks or len(tracks) != len(raw_tracks):
        return None
    series_id = value.get("series_id")
    if series_id is not None and (
        not isinstance(series_id, str) or not series_id.strip()
    ):
        return None
    return OpencastEpisode(
        tracks,
        series_id.strip() if isinstance(series_id, str) else None,
    )


def _cached_episode_entries(value: Any) -> dict[str, OpencastEpisode]:
    entries: dict[str, OpencastEpisode] = {}
    if (
        not isinstance(value, dict)
        or value.get("format") != OPENCAST_EPISODES_CACHE_FORMAT
        or not isinstance(value.get("episodes"), dict)
    ):
        return entries
    for episode_id, raw_episode in value["episodes"].items():
        if not isinstance(episode_id, str) or not episode_id:
            continue
        episode = episode_from_cache_data(raw_episode)
        if episode is not None:
            entries[episode_id] = episode
    return entries


def restore_cached_episodes(ctx: SyncContext, course_id: Any, value: Any) -> None:
    """Restore persisted episodes into the provider's runtime cache."""
    course_key = _course_id_key(course_id)
    for episode_id, episode in _cached_episode_entries(value).items():
        ctx.opencast_episode_cache.setdefault((course_key, episode_id), episode)


def cached_episodes_data(ctx: SyncContext, course_id: Any) -> dict[str, Any] | None:
    """Snapshot episodes discovered for one course during this run."""
    course_key = _course_id_key(course_id)
    entries = {
        episode_id: episode
        for (
            cached_course_id,
            episode_id,
        ), episode in ctx.opencast_episode_cache.items()
        if cached_course_id == course_key
        and course_key is not None
        and (course_key, episode_id) in ctx.opencast_seen_episodes
    }
    if not entries:
        return None
    return {
        "format": OPENCAST_EPISODES_CACHE_FORMAT,
        "episodes": {
            episode_id: episode_cache_data(episode)
            for episode_id, episode in sorted(entries.items())
        },
    }


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

    return _validated_checksum(checksum_type, checksum_value)


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
    raw_flavor = track.get("type")
    flavor_type = (
        raw_flavor.partition("/")[0].strip().casefold()
        if isinstance(raw_flavor, str)
        else ""
    )
    return OpencastTrack(
        url=url,
        checksum_type=checksum_type,
        checksum=checksum,
        size=optional_int(track.get("size")),
        duration=optional_int(track.get("duration")),
        flavor_type=flavor_type or None,
    )


def _mediapackage(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("mediapackage"), dict):
        return None
    return cast(dict[str, Any], entry["mediapackage"])


def _episode_track_data(entries: list[Any]) -> Iterator[dict[str, Any]]:
    for entry in entries:
        mediapackage = _mediapackage(entry)
        if mediapackage is None:
            continue
        media = mediapackage.get("media")
        track_data = media.get("track") if isinstance(media, dict) else None
        if isinstance(track_data, dict):
            yield track_data
        elif isinstance(track_data, list):
            yield from (track for track in track_data if isinstance(track, dict))


def _episode_cache_key(
    course_id: Any,
    episode_id: str,
) -> tuple[str | None, str]:
    return _course_id_key(course_id), episode_id


def _series_cache_key(
    course_id: Any,
    series_id: str,
) -> tuple[str | None, str]:
    return _course_id_key(course_id), series_id


def _cached_episode(
    ctx: SyncContext,
    course_id: Any,
    episode_id: str,
) -> OpencastEpisode | None:
    cache_key = _episode_cache_key(course_id, episode_id)
    if cache_key[0] is not None:
        ctx.opencast_seen_episodes.add((cache_key[0], episode_id))
    return ctx.opencast_episode_cache.get(cache_key)


def store_episode(
    ctx: SyncContext,
    course_id: Any,
    episode_id: str,
    episode: OpencastEpisode,
    *,
    state: OpencastMetadataState | None = OpencastMetadataState.FRESH,
    seen: bool = True,
) -> None:
    """Store an episode, leaving it unvalidated when ``state`` is ``None``."""
    cache_key = _episode_cache_key(course_id, episode_id)
    ctx.opencast_episode_cache[cache_key] = episode
    if state is None:
        ctx.opencast_metadata_states.pop(cache_key, None)
    else:
        ctx.opencast_metadata_states[cache_key] = state
    if seen and cache_key[0] is not None:
        ctx.opencast_seen_episodes.add((cache_key[0], episode_id))


def invalidate_episode(
    ctx: SyncContext,
    course_id: Any,
    episode_id: str,
    *,
    state: OpencastMetadataState | None = None,
) -> None:
    """Drop cached tracks and optionally record a terminal state for this run."""
    cache_key = _episode_cache_key(course_id, episode_id)
    ctx.opencast_episode_cache.pop(cache_key, None)
    if state is not None:
        ctx.opencast_metadata_states[cache_key] = state
    else:
        ctx.opencast_metadata_states.pop(cache_key, None)
    if cache_key[0] is not None:
        ctx.opencast_seen_episodes.discard((cache_key[0], episode_id))


def episode_metadata_is_stale(
    ctx: SyncContext,
    course_id: Any,
    episode_id: str,
) -> bool:
    return (
        ctx.opencast_metadata_states.get(_episode_cache_key(course_id, episode_id))
        is OpencastMetadataState.STALE
    )


def tracks_from_entries(entries: list[Any]) -> tuple[OpencastTrack, ...]:
    selected: dict[
        tuple[str, str], tuple[tuple[int, int, int, str], OpencastTrack]
    ] = {}
    for track_data in _episode_track_data(entries):
        track = opencast_track_from_api(track_data)
        if track is None:
            continue
        video = cast(dict[str, Any], track_data["video"])
        quality = (
            resolution_width(video.get("resolution")),
            optional_int(video.get("bitrate")) or 0,
            track.size or 0,
            track.url,
        )
        # Multiple encodings of a known logical flavor are renditions of the
        # same track. Without flavor metadata, retain every distinct URL rather
        # than silently treating unrelated videos as one generic track.
        track_key = (
            ("flavor", track.flavor_type)
            if track.flavor_type is not None
            else ("url", track.url)
        )
        current = selected.get(track_key)
        if current is None or quality > current[0]:
            selected[track_key] = (quality, track)

    return tuple(selected[track_key][1] for track_key in sorted(selected))


def _series_id_from_entries(
    entries: list[Any],
    fallback: str | None = None,
) -> str | None:
    for entry in entries:
        mediapackage = _mediapackage(entry)
        series_id = mediapackage.get("series") if mediapackage is not None else None
        if isinstance(series_id, str) and series_id.strip():
            return series_id.strip()
    return fallback


def _entries_include_media(entries: list[Any]) -> bool:
    for entry in entries:
        mediapackage = _mediapackage(entry)
        media = mediapackage.get("media") if mediapackage is not None else None
        tracks = media.get("track") if isinstance(media, dict) else None
        if isinstance(tracks, dict) or (
            isinstance(tracks, list)
            and all(isinstance(track, dict) for track in tracks)
        ):
            return True
    return False


def _cache_episode_entries(
    ctx: SyncContext,
    course_id: Any,
    episode_id: str,
    entries: list[Any],
    *,
    series_id: str | None = None,
    seen: bool = True,
) -> bool:
    if not _entries_include_media(entries):
        cache_key = _episode_cache_key(course_id, episode_id)
        ctx.opencast_metadata_states.pop(cache_key, None)
        cached = ctx.opencast_episode_cache.get(cache_key)
        if cached is not None and cached.series_id is None and series_id is not None:
            store_episode(
                ctx,
                course_id,
                episode_id,
                OpencastEpisode(cached.tracks, series_id),
                state=None,
                seen=seen,
            )
        return False

    tracks = tracks_from_entries(entries)
    if not tracks:
        invalidate_episode(
            ctx,
            course_id,
            episode_id,
            state=OpencastMetadataState.FRESH,
        )
        return False
    store_episode(
        ctx,
        course_id,
        episode_id,
        OpencastEpisode(
            tracks,
            _series_id_from_entries(entries, series_id),
        ),
        seen=seen,
    )
    return True


def _new_series_entries(
    series_id: str,
    page: list[Any],
    seen_episode_ids: set[str],
    log: logging.Logger,
) -> list[tuple[str, str, Any]]:
    entries: list[tuple[str, str, Any]] = []
    for entry in page:
        mediapackage = _mediapackage(entry)
        if mediapackage is None:
            log.warning(
                "Opencast: series %s contains episode without id",
                series_id,
            )
            continue
        episode_id = mediapackage.get("id")
        if not isinstance(episode_id, str) or not episode_id:
            log.warning(
                "Opencast: series %s contains episode without id",
                series_id,
            )
            continue
        if episode_id in seen_episode_ids:
            continue
        seen_episode_ids.add(episode_id)
        raw_title = mediapackage.get("title")
        title = raw_title if isinstance(raw_title, str) and raw_title else episode_id
        entries.append((episode_id, title, entry))
    return entries


def _cache_series_entries(
    ctx: SyncContext,
    course_id: Any,
    series_id: str,
    entries: list[tuple[str, str, Any]],
    complete: bool,
) -> None:
    episode_ids = {episode_id for episode_id, _, _ in entries}
    for episode_id, _, entry in entries:
        _cache_episode_entries(
            ctx,
            course_id,
            episode_id,
            [entry],
            series_id=series_id,
            seen=False,
        )

    if not complete:
        return
    cache_key = _series_cache_key(course_id, series_id)
    for episode_key, episode in list(ctx.opencast_episode_cache.items()):
        if (
            episode_key[0] == cache_key[0]
            and episode.series_id == series_id
            and episode_key[1] not in episode_ids
        ):
            invalidate_episode(
                ctx,
                episode_key[0],
                episode_key[1],
                state=OpencastMetadataState.FRESH,
            )


def list_series_episodes(
    ctx: SyncContext,
    series_id: str,
    log: logging.Logger = logger,
    course_id: Any = None,
) -> tuple[tuple[str, str], ...] | None:
    """Fetch a series once per course and cache all usable episode metadata."""
    cache_key = _series_cache_key(course_id, series_id)
    if cache_key in ctx.opencast_series_cache:
        return ctx.opencast_series_cache[cache_key]

    entries: list[tuple[str, str, Any]] = []
    seen_episode_ids: set[str] = set()
    offset = 0
    complete = False
    can_prove_complete = True
    while True:
        ctx.output.sync_progress.module_status(
            f"listing Opencast episodes ({len(entries)} found)"
        )
        query = urllib.parse.urlencode(
            {
                "limit": OPENCAST_SERIES_PAGE_SIZE,
                "offset": offset,
                "sid": series_id,
            }
        )
        page = fetch_result_list(
            ctx,
            f"{OPENCAST_SEARCH_URL}?{query}",
            f"series {series_id}",
            log,
        )
        if page is None:
            ctx.opencast_series_cache[cache_key] = None
            return None

        new_entries = _new_series_entries(series_id, page, seen_episode_ids, log)
        can_prove_complete &= len(new_entries) == len(page)
        if page and not new_entries:
            log.warning(
                "Opencast: series %s made no pagination progress at offset %s; "
                "stopping",
                series_id,
                offset,
            )
            break
        entries.extend(new_entries)
        if len(page) < OPENCAST_SERIES_PAGE_SIZE:
            complete = can_prove_complete
            break
        offset += OPENCAST_SERIES_PAGE_SIZE

    if not complete:
        ctx.opencast_series_cache[cache_key] = None
        return None
    _cache_series_entries(ctx, course_id, series_id, entries, True)
    result = tuple((episode_id, title) for episode_id, title, _ in entries)
    ctx.opencast_series_cache[cache_key] = result
    return result


def _authorize_episode_refresh(
    ctx: SyncContext,
    course_id: Any,
    episode_id: str,
    log: logging.Logger,
) -> bool:
    if course_id is None:
        return True
    if not course_is_authorized(ctx, course_id):
        ctx.output.sync_progress.module_status("authorizing Opencast course")
    return authorize_course_for_episode(ctx, course_id, episode_id, log)


def _stale_episode(
    ctx: SyncContext,
    course_id: Any,
    episode_id: str,
    cached: OpencastEpisode | None,
    log: logging.Logger,
) -> OpencastEpisode | None:
    cache_key = _episode_cache_key(course_id, episode_id)
    if ctx.opencast_metadata_states.get(cache_key) is OpencastMetadataState.STALE:
        return cached
    ctx.opencast_metadata_states[cache_key] = OpencastMetadataState.STALE
    if cached is not None:
        log.warning(
            "Opencast: could not refresh metadata for %s; cached metadata will "
            "only be used to preserve existing files",
            episode_id,
        )
    return cached


def _refresh_episode(
    ctx: SyncContext,
    course_id: Any,
    episode_id: str,
    cached: OpencastEpisode | None,
    log: logging.Logger,
) -> OpencastEpisode | None:
    ctx.output.sync_progress.module_status("resolving Opencast video")
    entries = fetch_result_list(
        ctx,
        f"{OPENCAST_SEARCH_URL}?id={episode_id}",
        f"episode {episode_id}",
        log,
    )
    if entries is None:
        return _stale_episode(ctx, course_id, episode_id, cached, log)
    if not entries:
        invalidate_episode(
            ctx,
            course_id,
            episode_id,
            state=OpencastMetadataState.FRESH,
        )
        log.warning("Opencast: no downloadable mp4 track found for %s", episode_id)
        return None
    if not _entries_include_media(entries):
        log.warning("Opencast: metadata for %s did not include media", episode_id)
        return _stale_episode(ctx, course_id, episode_id, cached, log)
    if not _cache_episode_entries(ctx, course_id, episode_id, entries):
        log.warning("Opencast: no downloadable mp4 track found for %s", episode_id)
        return None
    return _cached_episode(ctx, course_id, episode_id)


def resolve_tracks_from_episode(
    ctx: SyncContext,
    episode_id: str,
    log: logging.Logger = logger,
    *,
    course_id: Any = None,
) -> tuple[OpencastTrack, ...] | None:
    """Return tracks after one authoritative refresh for their mutable scope."""
    cache_key = _episode_cache_key(course_id, episode_id)
    cached = _cached_episode(ctx, course_id, episode_id)
    if cache_key in ctx.opencast_metadata_states:
        return cached.tracks if cached is not None else None
    if not _authorize_episode_refresh(ctx, course_id, episode_id, log):
        stale = _stale_episode(ctx, course_id, episode_id, cached, log)
        return stale.tracks if stale is not None else None

    if cached is not None and cached.series_id is not None:
        list_series_episodes(ctx, cached.series_id, log, course_id)
        cached = _cached_episode(ctx, course_id, episode_id)
        if cache_key in ctx.opencast_metadata_states:
            return cached.tracks if cached is not None else None

    refreshed = _refresh_episode(ctx, course_id, episode_id, cached, log)
    return refreshed.tracks if refreshed is not None else None
