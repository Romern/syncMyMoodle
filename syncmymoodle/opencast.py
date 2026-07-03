import json
import logging
import re
import urllib.parse
from typing import Any, cast

from bs4 import BeautifulSoup as bs

from syncmymoodle.constants import MOODLE_URL, RWTH_MOODLE_STATUS_URL
from syncmymoodle.context import SyncContext

logger = logging.getLogger(__name__)

OPENCAST_LTI_URL = "https://engage.streaming.rwth-aachen.de/lti"
OPENCAST_SEARCH_URL = "https://engage.streaming.rwth-aachen.de/search/episode.json"


def log_backend_issue(
    ctx: SyncContext,
    response_body: str | None = None,
    log: logging.Logger = logger,
) -> None:
    """Log additional context for repeated Opencast backend issues.

    We keep the response body at INFO level (only shown with --verbose) and
    emit a hint to the RWTH ITC status page once the error counter exceeds a
    small threshold.
    """
    ctx.opencast_error_count += 1

    if response_body:
        log.info(f"Opencast response body (truncated): {response_body[:1000]}")

    if ctx.opencast_error_count >= 5 and not ctx.opencast_status_hint_logged:
        log.warning(
            "Multiple Opencast backend errors occurred. Please check the RWTH "
            "ITC status page before reporting an issue on GitHub: "
            f"{RWTH_MOODLE_STATUS_URL}"
        )
        ctx.opencast_status_hint_logged = True


def extract_episode_id(url: Any) -> str | None:
    if not url:
        return None

    url = str(url).replace("&amp;", "&")
    parsed = urllib.parse.urlparse(url)
    episode_ids = urllib.parse.parse_qs(parsed.query).get("episodeid", [])
    if episode_ids and episode_ids[0]:
        return str(episode_ids[0])

    match = re.match(
        r"^https://engage\.streaming\.rwth-aachen\.de/play/([a-zA-Z0-9-]{36})(?:[/?#].*)?$",
        url,
    )
    if match:
        return match.group(1)

    return None


def extract_lti_form_data(soup: Any) -> dict[str, Any]:
    return {
        input_tag["name"]: input_tag.get("value", "")
        for input_tag in soup.find_all("input")
        if input_tag.get("name")
    }


def get_input_value(soup: Any, name: str) -> str | None:
    input_tag = soup.find("input", {"name": name})
    if input_tag and input_tag.get("value"):
        return cast(str, input_tag["value"])
    return None


def submit_lti_form(
    ctx: SyncContext,
    engage_data: dict[str, Any],
    context: str,
    log: logging.Logger = logger,
) -> bool:
    if not engage_data:
        log.warning("Opencast: missing LTI form fields for %s", context)
        return False

    try:
        response = ctx.require_session().post(OPENCAST_LTI_URL, data=engage_data)
    except Exception:
        log.exception("Opencast: failed to submit LTI form for %s", context)
        log_backend_issue(ctx, None, log)
        return False

    if not (200 <= response.status_code < 300):
        log.warning(
            "Opencast: LTI form returned status %s for %s",
            response.status_code,
            context,
        )
        log_backend_issue(ctx, response.text, log)
        return False

    return True


def fetch_lti_form_data(
    ctx: SyncContext,
    url: str,
    context: str,
    log: logging.Logger = logger,
) -> dict[str, Any] | None:
    try:
        response = ctx.require_session().get(url)
    except Exception:
        log.exception("Opencast: failed to fetch LTI form for %s", context)
        log_backend_issue(ctx, None, log)
        return None

    if not (200 <= response.status_code < 300):
        log.warning(
            "Opencast: LTI form returned status %s for %s",
            response.status_code,
            context,
        )
        log_backend_issue(ctx, response.text, log)
        return None

    soup = bs(response.text, features="lxml")
    engage_data = extract_lti_form_data(soup)
    if not engage_data:
        log.info("Opencast: no LTI form fields found for %s", context)
        log.info("------LTI-ERROR-HTML------")
        log.info(f"url: {url}")
        log.info(soup)
        return None

    return engage_data


def authenticate_episode(
    ctx: SyncContext,
    course_id: Any,
    episode_id: str,
    log: logging.Logger = logger,
) -> bool:
    if not ctx.session_key:
        log.warning("Opencast: cannot launch episode without Moodle sesskey")
        return False

    cache_key = (course_id, episode_id)
    if cache_key in ctx.opencast_episode_auth_cache:
        return True

    params = urllib.parse.urlencode(
        {
            "courseid": course_id,
            "episodeid": episode_id,
            "sesskey": ctx.session_key,
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


def fetch_json(
    ctx: SyncContext,
    url: str,
    context: str,
    log: logging.Logger = logger,
) -> dict[str, Any] | None:
    try:
        response = ctx.require_session().get(url)
    except Exception:
        log.exception("Opencast: failed to fetch %s from %s", context, url)
        log_backend_issue(ctx, None, log)
        return None

    if not (200 <= response.status_code < 300):
        log.error(
            "Opencast: %s returned status %s for %s",
            context,
            response.status_code,
            url,
        )
        log_backend_issue(ctx, response.text, log)
        return None

    try:
        payload = response.json()
    except ValueError:
        log.error("Opencast: failed to decode JSON for %s from %s", context, url)
        log_backend_issue(ctx, response.text, log)
        return None

    if not isinstance(payload, dict):
        log.warning(
            "Opencast: expected JSON object for %s, got %s",
            context,
            type(payload).__name__,
        )
        log_backend_issue(ctx, response.text, log)
        return None

    if payload.get("error") or payload.get("errorcode"):
        log.error(
            "Opencast: %s returned error%s: %s",
            context,
            f" {payload.get('errorcode')}" if payload.get("errorcode") else "",
            payload.get("error") or payload,
        )
        log_backend_issue(ctx, response.text, log)
        return None

    return payload


def get_result_list(
    ctx: SyncContext,
    payload: Any,
    context: str,
    log: logging.Logger = logger,
) -> list[Any]:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, list):
        log.warning("Opencast: missing result list for %s", context)
        log_backend_issue(
            ctx,
            json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            log,
        )
        return []
    if not result:
        log.warning("Opencast: empty result list for %s", context)
        return []
    return result


def resolution_width(resolution: Any) -> int:
    match = re.match(r"(\d+)\s*x\s*\d+", str(resolution or ""))
    if not match:
        return 0
    return int(match.group(1))


def extract_track_from_episode(
    ctx: SyncContext,
    episode_id: str,
    log: logging.Logger = logger,
) -> str | bool:
    if episode_id in ctx.opencast_track_cache:
        return ctx.opencast_track_cache[episode_id]

    episode_url = f"{OPENCAST_SEARCH_URL}?id={episode_id}"
    episodejson = fetch_json(ctx, episode_url, f"episode {episode_id}", log)
    if episodejson is None:
        return False

    tracks: list[tuple[int, str]] = []
    for entry in get_result_list(ctx, episodejson, f"episode {episode_id}", log):
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
            video = track.get("video")
            url = track.get("url")
            if (
                url
                and track.get("mimetype") == "video/mp4"
                and "transport" not in track
                and isinstance(video, dict)
            ):
                tracks.append((resolution_width(video.get("resolution")), url))

    if not tracks:
        log.warning("Opencast: no downloadable mp4 track found for %s", episode_id)
        return False

    # Prefer the highest resolution plain HTTPS mp4 track.
    track_url = sorted(tracks, key=lambda track: track[0])[-1][1]
    ctx.opencast_track_cache[episode_id] = track_url
    return track_url
