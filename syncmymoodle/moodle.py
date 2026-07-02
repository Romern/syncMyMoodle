import base64
import http.client
import json
import logging
import sys
import urllib.parse
from typing import Any

import requests

logger = logging.getLogger(__name__)

MOODLE_HOST = "moodle.rwth-aachen.de"
MOODLE_REST_URL = f"https://{MOODLE_HOST}/webservice/rest/server.php"


def _cookie_header(cookie_jar: Any, domain: str) -> str:
    # workaround for macos
    cookie_dict = cookie_jar.get_dict(domain=domain)
    found = ["%s=%s" % (name, value) for (name, value) in cookie_dict.items()]
    return ";".join(found)


def get_moodle_wstoken(
    session: requests.Session | None, log: logging.Logger = logger
) -> str:
    if not session:
        raise Exception("You need to login() first.")
    params = {
        "service": "moodle_mobile_app",
        "passport": 1,
        "urlscheme": "moodlemobile",
    }
    # response = session.head("https://moodle.rwth-aachen.de/admin/tool/mobile/launch.php", params=params, allow_redirects=False)

    conn = http.client.HTTPSConnection(MOODLE_HOST)
    conn.request(
        "GET",
        "/admin/tool/mobile/launch.php?" + urllib.parse.urlencode(params),
        headers={"Cookie": _cookie_header(session.cookies, MOODLE_HOST)},
    )
    response = conn.getresponse()

    # token is in an app schema, which contains the wstoken base64-encoded along with some other token
    location = response.getheader("Location")
    if location is None or "token=" not in location:
        location_path = urllib.parse.urlparse(location).path if location else None
        body_prefix = response.read(1000).decode("utf-8", errors="replace")
        conn.close()

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
            "Unexpected mobile launch redirect target: "
            f"{location_path or '<missing>'}"
        )
        if body_prefix:
            log.info(
                "Unexpected mobile launch response body (truncated): " f"{body_prefix}"
            )
        sys.exit(1)

    # The redirect looks like moodlemobile://token=BASE64[&...]; isolate the
    # token value and decode it defensively so a malformed redirect yields a
    # clear message instead of a traceback.
    token_base64d = location.split("token=", 1)[1].split("&")[0]
    conn.close()
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


def get_all_courses(session: requests.Session, wstoken: str, user_id: Any) -> Any:
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
    return json.loads(resp.json()["responses"][0]["data"])


def get_course(session: requests.Session, wstoken: str, course_id: Any) -> Any:
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
    return resp.json()


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
    if not payload.get("userid") or not payload["userprivateaccesskey"]:
        log.critical(
            f"Error while getting userid and access key: {json.dumps(payload, indent=4)}"
        )
        sys.exit(1)
    return payload["userid"], payload["userprivateaccesskey"]


def get_assignment(session: requests.Session, wstoken: str, course_id: Any) -> Any:
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
    courses = resp.json()["courses"]
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
    files = payload.get("lastattempt", {}).get("submission", {}).get("plugins", [])
    files += payload.get("lastattempt", {}).get("teamsubmission", {}).get("plugins", [])
    files += payload.get("feedback", {}).get("plugins", [])

    files = [
        f.get("files", [])
        for p in files
        for f in p.get("fileareas", [])
        if f["area"] in ["download", "submission_files", "feedback_files"]
    ]
    files = [f for folder in files for f in folder]
    return files


def get_folders_by_courses(
    session: requests.Session, wstoken: str, course_id: Any
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
    folder = response.json()["folders"]
    return folder
