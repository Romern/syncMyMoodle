import base64
import json
import logging
import sys
import urllib.parse
from typing import Any

import requests

logger = logging.getLogger(__name__)

MOODLE_HOST = "moodle.rwth-aachen.de"
MOODLE_REST_URL = f"https://{MOODLE_HOST}/webservice/rest/server.php"
MOODLE_MOBILE_LAUNCH_URL = f"https://{MOODLE_HOST}/admin/tool/mobile/launch.php"


def get_moodle_wstoken(
    session: requests.Session | None, log: logging.Logger = logger
) -> str:
    if not session:
        raise Exception("You need to login() first.")
    params = {
        "service": "moodle_mobile_app",
        "passport": "1",
        "urlscheme": "moodlemobile",
    }
    # The launch endpoint answers with a redirect to a non-HTTP app scheme,
    # moodlemobile://token=BASE64[&...]. Do NOT follow it: making requests
    # resolve the redirect makes it look for a connection adapter for the
    # moodlemobile:// scheme and raise InvalidSchema.
    response = session.get(
        MOODLE_MOBILE_LAUNCH_URL, params=params, allow_redirects=False
    )

    # token is in an app schema, which contains the wstoken base64-encoded along with some other token
    location = response.headers.get("Location")
    if location is None or "token=" not in location:
        location_path = urllib.parse.urlparse(location).path if location else None
        body_prefix = response.text[:1000]

        if location_path and location_path.startswith("/admin/tool/policy/"):
            log.critical(
                "RWTHmoodle requires you to accept updated policies/terms "
                "before syncmymoodle can create a webservice token. Please "
                "open https://moodle.rwth-aachen.de/ in your browser, accept "
                "the pending policy page, and rerun syncmymoodle."
            )
            log.info(
                "Unexpected mobile launch redirect target: "
                f"{location_path or '<missing>'}"
            )
            sys.exit(1)

        if location_path == "/login/index.php":
            log.critical(
                "Failed to retrieve the Moodle webservice token because "
                "Moodle redirected back to the login page. Your saved "
                "session is probably stale or the SSO login did not finish "
                "correctly. Delete the cookie file and try again."
            )
            log.info(
                "Unexpected mobile launch redirect target: "
                f"{location_path or '<missing>'}"
            )
            sys.exit(1)

        log.critical(
            "Failed to retrieve the Moodle webservice token because Moodle "
            "returned an unexpected redirect instead of a token."
        )
        log.info(
            f"Unexpected mobile launch redirect target: {location_path or '<missing>'}"
        )
        if body_prefix:
            log.info(
                f"Unexpected mobile launch response body (truncated): {body_prefix}"
            )
        sys.exit(1)

    # The redirect looks like moodlemobile://token=BASE64[&...]; isolate the
    # token value and decode it defensively so a malformed redirect yields a
    # clear message instead of a traceback.
    token_base64d = location.split("token=", 1)[1].split("&")[0]
    try:
        token_parts = base64.b64decode(token_base64d).decode().split(":::")
    except (ValueError, UnicodeDecodeError):
        token_parts = []
    if len(token_parts) < 2 or not token_parts[1]:
        log.critical(
            "Failed to parse the Moodle webservice token from the mobile "
            "launch redirect. Your saved session may be stale; delete the "
            "cookie file and try again."
        )
        sys.exit(1)
    return token_parts[1]


def api_error_message(payload: Any) -> str | None:
    """Return a description when ``payload`` is a Moodle error object.

    Moodle webservice endpoints answer errors (expired token, missing
    permission, hidden course, ...) with a JSON object carrying ``exception``/
    ``errorcode`` instead of the expected data. Callers previously indexed
    straight into the expected shape, turning such errors into KeyError
    tracebacks.
    """
    if not isinstance(payload, dict):
        return None
    if not (payload.get("exception") or payload.get("errorcode")):
        return None
    message = payload.get("message") or payload.get("exception") or "unknown error"
    errorcode = payload.get("errorcode")
    return f"{message} (errorcode: {errorcode})" if errorcode else str(message)


def get_all_courses(
    session: requests.Session,
    wstoken: str,
    user_id: Any,
    log: logging.Logger = logger,
) -> Any:
    data = {
        "requests[0][function]": "core_enrol_get_users_courses",
        "requests[0][arguments]": json.dumps(
            {"userid": str(user_id), "returnusercount": "0"}
        ),
        "requests[0][settingfilter]": 1,
        "requests[0][settingfileurl]": 1,
        "wsfunction": "tool_mobile_call_external_functions",
        "wstoken": wstoken,
    }
    params = {
        "moodlewsrestformat": "json",
        "wsfunction": "tool_mobile_call_external_functions",
    }
    resp = session.post(MOODLE_REST_URL, params=params, data=data)
    payload = resp.json()

    # Without the course list nothing can be synced, so API errors here are
    # fatal. A clear message beats the KeyError traceback this used to raise.
    error = api_error_message(payload)
    if error is None:
        responses = payload.get("responses") if isinstance(payload, dict) else None
        first = responses[0] if responses else None
        if first is None:
            error = "unexpected response shape"
        elif first.get("error"):
            error = str(first.get("exception") or first.get("data"))
        else:
            return json.loads(first["data"])
    log.critical(
        "Failed to retrieve the course list from Moodle: %s. Your session "
        "or webservice token may have expired; delete the cookie file and "
        "try again.",
        error,
    )
    sys.exit(1)


def get_course(
    session: requests.Session,
    wstoken: str,
    course_id: Any,
    log: logging.Logger = logger,
) -> Any:
    data = {
        "courseid": int(course_id),
        "moodlewssettingfilter": True,
        "moodlewssettingfileurl": True,
        "wsfunction": "core_course_get_contents",
        "wstoken": wstoken,
    }
    params = {
        "moodlewsrestformat": "json",
        "wsfunction": "core_course_get_contents",
    }
    resp = session.post(MOODLE_REST_URL, params=params, data=data)
    payload = resp.json()
    # A course-specific error (hidden course, missing permission) should skip
    # this course but not abort the whole sync.
    error = api_error_message(payload)
    if error is not None:
        log.error("Skipping course %s: %s", course_id, error)
        return []
    return payload


def get_userid(
    session: requests.Session,
    wstoken: str,
    log: logging.Logger = logger,
) -> tuple[Any, str]:
    data = {
        "moodlewssettingfilter": True,
        "moodlewssettingfileurl": True,
        "wsfunction": "core_webservice_get_site_info",
        "wstoken": wstoken,
    }
    params = {
        "moodlewsrestformat": "json",
        "wsfunction": "core_webservice_get_site_info",
    }
    resp = session.post(MOODLE_REST_URL, params=params, data=data)
    payload = resp.json()
    if not payload.get("userid") or not payload.get("userprivateaccesskey"):
        log.critical(
            f"Error while getting userid and access key: {json.dumps(payload, indent=4)}"
        )
        sys.exit(1)
    return payload["userid"], payload["userprivateaccesskey"]


def get_assignment(
    session: requests.Session,
    wstoken: str,
    course_id: Any,
    log: logging.Logger = logger,
) -> Any:
    data = {
        "courseids[0]": int(course_id),
        "includenotenrolledcourses": 1,
        "moodlewssettingfilter": True,
        "moodlewssettingfileurl": True,
        "wsfunction": "mod_assign_get_assignments",
        "wstoken": wstoken,
    }
    params = {
        "moodlewsrestformat": "json",
        "wsfunction": "mod_assign_get_assignments",
    }
    resp = session.post(MOODLE_REST_URL, params=params, data=data)
    payload = resp.json()
    error = api_error_message(payload)
    if error is not None:
        log.error("Skipping assignments for course %s: %s", course_id, error)
        return None
    courses = payload.get("courses") or []
    return courses[0] if courses else None


def get_assignment_submission_files(
    session: requests.Session,
    wstoken: str,
    user_id: Any,
    assignment_id: Any,
    log: logging.Logger = logger,
) -> list[Any]:
    data = {
        "assignid": assignment_id,
        "userid": user_id,
        "moodlewssettingfilter": True,
        "moodlewssettingfileurl": True,
        "wsfunction": "mod_assign_get_submission_status",
        "wstoken": wstoken,
    }

    params = {
        "moodlewsrestformat": "json",
        "wsfunction": "mod_assign_get_submission_status",
    }

    response = session.post(MOODLE_REST_URL, params=params, data=data)

    log.info(f"------ASSIGNMENT-{assignment_id}-DATA------")
    log.info(response.text)

    payload = response.json()
    # Per-assignment errors (e.g. no permission to view the submission) keep
    # their historical behavior of contributing no files. The "or {}" also
    # guards against JSON null values, which .get(key, {}) does not.
    lastattempt = payload.get("lastattempt") or {}
    files = (lastattempt.get("submission") or {}).get("plugins") or []
    files += (lastattempt.get("teamsubmission") or {}).get("plugins") or []
    files += (payload.get("feedback") or {}).get("plugins") or []

    files = [
        f.get("files", [])
        for p in files
        for f in p.get("fileareas", [])
        if f.get("area") in ["download", "submission_files", "feedback_files"]
    ]
    files = [f for folder in files for f in folder]
    return files


def get_folders_by_courses(
    session: requests.Session,
    wstoken: str,
    course_id: Any,
    log: logging.Logger = logger,
) -> Any:
    data = {
        "courseids[0]": str(course_id),
        "moodlewssettingfilter": True,
        "moodlewssettingfileurl": True,
        "wsfunction": "mod_folder_get_folders_by_courses",
        "wstoken": wstoken,
    }

    params = {
        "moodlewsrestformat": "json",
        "wsfunction": "mod_folder_get_folders_by_courses",
    }

    response = session.post(MOODLE_REST_URL, params=params, data=data)
    payload = response.json()
    error = api_error_message(payload)
    if error is not None:
        log.error("Skipping folders for course %s: %s", course_id, error)
        return []
    return payload.get("folders") or []
