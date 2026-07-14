import base64
import binascii
import hashlib
import json
import logging
import secrets
import sys
import urllib.parse
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

import requests
from requests.auth import AuthBase

from syncmymoodle.constants import HTTP_TIMEOUT_SECONDS, MOODLE_URL
from syncmymoodle.http_utils import (
    HttpFailureKind,
    classify_http_failure,
    moodle_user_id_from_html,
    parse_html,
    request_following_safe_redirects,
    safe_error_message,
    same_origin,
    session_key_from_html,
)
from syncmymoodle.moodle_tokens import MoodleTokens, normalized_site

logger = logging.getLogger(__name__)

MOODLE_REST_URL = f"{MOODLE_URL}webservice/rest/server.php"
MOODLE_MOBILE_LAUNCH_URL = f"{MOODLE_URL}admin/tool/mobile/launch.php"
MOODLE_MANAGE_TOKEN_URL = f"{MOODLE_URL}user/managetoken.php"
MOBILE_URL_SCHEME = "syncmymoodle"
MOODLE_MOBILE_USER_AGENT = "MoodleMobile syncMyMoodle"
MOODLE_UPDATE_FUNCTION = "core_course_get_updates_since"
MOODLE_UPDATE_OVERLAP_SECONDS = 5


class MobileLaunchError(RuntimeError):
    pass


class MobileTokenResetError(RuntimeError):
    pass


class BrowserBootstrapError(RuntimeError):
    pass


class BrowserSessionIdentityError(RuntimeError):
    pass


class MoodleTokenAuth(AuthBase):
    """Attach the endpoint-specific token to RWTH Moodle file requests."""

    def __init__(
        self,
        wstoken: str,
        user_private_access_key: str | None = None,
    ) -> None:
        self.wstoken = wstoken
        self.user_private_access_key = user_private_access_key

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        if request.url is None:
            return request
        parsed = urllib.parse.urlsplit(request.url)
        if not same_origin(request.url, MOODLE_URL):
            return request
        path = parsed.path
        if path.startswith("/pluginfile.php/"):
            path = "/webservice" + path
        token: str | None
        if path.startswith("/webservice/pluginfile.php/"):
            token = self.wstoken
        elif path.startswith("/tokenpluginfile.php/"):
            token = self.user_private_access_key
            if token is None:
                return request
        else:
            return request
        query = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True
            )
            if key.lower() not in {"token", "wstoken"}
        ]
        query.append(("token", token))
        request.url = urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                path,
                urllib.parse.urlencode(query),
                parsed.fragment,
            )
        )
        return request


def create_token_session(
    tokens: MoodleTokens,
    user_private_access_key: str | None = None,
) -> requests.Session:
    session = requests.Session()
    session.auth = MoodleTokenAuth(tokens.wstoken, user_private_access_key)
    return session


def _http_date_timestamp(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    try:
        timestamp = int(parsed.timestamp())
    except (OSError, OverflowError, ValueError):
        return None
    return timestamp if timestamp >= 0 else None


class TokenValidationKind(Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TokenValidation:
    kind: TokenValidationKind
    detail: str | None = None
    site_info: dict[str, Any] | None = None
    server_time: int | None = None


@dataclass(frozen=True)
class CourseUpdates:
    """A conservative view of Moodle's per-module update feed."""

    since: int
    changed_module_ids: frozenset[int]
    unknown_module_ids: frozenset[int]

    def confirms_unchanged(self, module_id: int, cached_since: int) -> bool:
        return (
            cached_since >= self.since
            and module_id not in self.changed_module_ids
            and module_id not in self.unknown_module_ids
        )


def mobile_site_signature(passport: str, site: str = MOODLE_URL) -> str:
    server_site = site.rstrip("/")
    return hashlib.md5(
        f"{server_site}{passport}".encode(), usedforsecurity=False
    ).hexdigest()


def parse_mobile_launch_location(
    location: str,
    passport: str,
    username: str,
    site: str = MOODLE_URL,
    moodle_user_id: int | None = None,
) -> MoodleTokens:
    prefix = f"{MOBILE_URL_SCHEME}://token="
    if not location.startswith(prefix):
        raise MobileLaunchError("Moodle returned an unexpected mobile launch redirect")
    encoded = location[len(prefix) :].split("&", 1)[0]
    try:
        decoded = base64.b64decode(encoded, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as error:
        raise MobileLaunchError("Moodle returned a malformed token response") from error
    parts = decoded.split(":::")
    if len(parts) not in {2, 3} or not parts[1]:
        raise MobileLaunchError("Moodle returned an incomplete token response")
    if parts[0] != mobile_site_signature(passport, site):
        raise MobileLaunchError("Moodle mobile response correlation check failed")
    private_token = parts[2] if len(parts) == 3 else ""
    return MoodleTokens(
        username=username,
        wstoken=parts[1],
        private_token=private_token or None,
        site=site,
        moodle_user_id=moodle_user_id,
    )


def acquire_mobile_tokens(
    session: requests.Session | None,
    username: str,
    *,
    passport: str | None = None,
) -> MoodleTokens:
    if session is None:
        raise MobileLaunchError("a logged-in Moodle session is required")
    try:
        moodle_user_id = browser_session_user_id(session)
    except BrowserSessionIdentityError as error:
        raise MobileLaunchError(str(error)) from error
    passport = passport or secrets.token_hex(16)
    try:
        response = session.get(
            MOODLE_MOBILE_LAUNCH_URL,
            params={
                "service": "moodle_mobile_app",
                "passport": passport,
                "urlscheme": MOBILE_URL_SCHEME,
            },
            allow_redirects=False,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise MobileLaunchError(
            f"could not request Moodle tokens: {safe_error_message(error)}"
        ) from None
    location = response.headers.get("Location")
    if location is None:
        raise MobileLaunchError("Moodle mobile launch response had no redirect")
    return parse_mobile_launch_location(
        location,
        passport,
        username,
        moodle_user_id=moodle_user_id,
    )


def browser_session_user_id(session: requests.Session) -> int:
    try:
        response = session.get(MOODLE_URL, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as error:
        raise BrowserSessionIdentityError(
            "could not verify the logged-in Moodle account: "
            f"{safe_error_message(error)}"
        ) from None
    user_id = moodle_user_id_from_html(response.text)
    if user_id is None:
        raise BrowserSessionIdentityError(
            "logged-in Moodle session did not expose a valid user id"
        )
    return user_id


def mobile_token_id_from_security_keys(html: str) -> str:
    soup = parse_html(html)
    for link in soup.select('a[href*="action=resetwstoken"]'):
        row = link.find_parent("tr")
        if row is None or "Moodle mobile web service" not in row.get_text(
            " ", strip=True
        ):
            continue
        href = link.get("href")
        if not isinstance(href, str):
            continue
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        token_ids = query.get("tokenid")
        if token_ids and token_ids[0]:
            return str(token_ids[0])
    raise MobileTokenResetError(
        "could not find the Moodle mobile web service on the Security keys page"
    )


def reset_mobile_token(session: requests.Session, session_key: str) -> None:
    try:
        response = session.get(
            MOODLE_MANAGE_TOKEN_URL,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        token_id = mobile_token_id_from_security_keys(response.text)
        response = session.get(
            MOODLE_MANAGE_TOKEN_URL,
            params={
                "action": "resetwstoken",
                "tokenid": token_id,
                "confirm": "1",
                "sesskey": session_key,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise MobileTokenResetError(
            f"could not reset Moodle API token: {safe_error_message(error)}"
        ) from None
    if not (200 <= response.status_code < 300):
        raise MobileTokenResetError(
            f"Moodle API token reset returned status {response.status_code}"
        )


def validate_mobile_tokens(
    tokens: MoodleTokens,
    *,
    session: requests.Session | None = None,
) -> TokenValidation:
    session = requests.Session() if session is None else session
    try:
        response = session.post(
            MOODLE_REST_URL,
            params={
                "moodlewsrestformat": "json",
                "wsfunction": "core_webservice_get_site_info",
            },
            data={
                "wstoken": tokens.wstoken,
                "wsfunction": "core_webservice_get_site_info",
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        return TokenValidation(TokenValidationKind.UNKNOWN, safe_error_message(error))
    failure_kind = classify_http_failure(response.status_code)
    if failure_kind is HttpFailureKind.TRANSIENT:
        return TokenValidation(
            TokenValidationKind.UNKNOWN,
            f"Moodle token validation returned HTTP {response.status_code}",
        )
    try:
        payload = response.json()
    except ValueError as error:
        return TokenValidation(TokenValidationKind.UNKNOWN, safe_error_message(error))
    if failure_kind is not None:
        api_error = api_error_message(payload)
        if isinstance(payload, dict) and payload.get("errorcode") == "invalidtoken":
            return TokenValidation(
                TokenValidationKind.INVALID,
                api_error or "Moodle rejected the token",
            )
        return TokenValidation(
            TokenValidationKind.UNKNOWN,
            f"Moodle token validation returned HTTP {response.status_code}",
        )
    return validate_mobile_token_payload(
        payload,
        tokens,
        server_time=_http_date_timestamp(response.headers.get("Date")),
    )


def validate_mobile_token_payload(
    payload: Any,
    tokens: MoodleTokens,
    *,
    server_time: int | None = None,
) -> TokenValidation:
    if not isinstance(payload, dict):
        return TokenValidation(TokenValidationKind.UNKNOWN, "unexpected response shape")
    api_error = api_error_message(payload)
    if api_error is not None:
        error_code = payload.get("errorcode")
        kind = (
            TokenValidationKind.INVALID
            if error_code == "invalidtoken"
            else TokenValidationKind.UNKNOWN
        )
        return TokenValidation(kind, api_error)
    user_id = payload.get("userid")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return TokenValidation(
            TokenValidationKind.UNKNOWN, "site info omitted a valid user id"
        )
    site_url = payload.get("siteurl")
    if not isinstance(site_url, str) or not site_url:
        return TokenValidation(
            TokenValidationKind.UNKNOWN, "site info omitted the Moodle site URL"
        )
    if normalized_site(site_url) != normalized_site(tokens.site):
        return TokenValidation(
            TokenValidationKind.INVALID, "token belongs to another site"
        )
    if tokens.moodle_user_id is None:
        return TokenValidation(
            TokenValidationKind.INVALID,
            "Moodle tokens have no verified Moodle account identity",
        )
    if user_id != tokens.moodle_user_id:
        return TokenValidation(
            TokenValidationKind.INVALID,
            "token belongs to another Moodle account",
        )
    return TokenValidation(
        TokenValidationKind.VALID,
        site_info=payload,
        server_time=server_time,
    )


def _open_moodle_autologin(
    autologin_url: str,
    user_id: int,
    key: str,
) -> tuple[requests.Session, str]:
    if not same_origin(autologin_url, MOODLE_URL):
        raise BrowserBootstrapError("Moodle returned an unsafe auto-login URL")
    browser_session = requests.Session()
    try:
        response = request_following_safe_redirects(
            browser_session,
            "GET",
            autologin_url,
            lambda url: same_origin(url, MOODLE_URL),
            params={"userid": str(user_id), "key": key},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise BrowserBootstrapError(
            "could not open the Moodle browser auto-login URL: "
            f"{safe_error_message(error)}"
        ) from None
    session_key = session_key_from_html(response.text)
    if session_key is None:
        raise BrowserBootstrapError(
            "Moodle auto-login did not create a browser session"
        )
    return browser_session, session_key


def create_browser_session(
    tokens: MoodleTokens,
) -> tuple[requests.Session, str]:
    if tokens.private_token is None:
        raise BrowserBootstrapError(
            "stored Moodle tokens have no browser login token; run "
            "`syncmymoodle auth reset-token`"
        )
    user_id = tokens.moodle_user_id
    if user_id is None:
        raise BrowserBootstrapError(
            "stored Moodle tokens have no verified Moodle account identity"
        )
    mobile_session = requests.Session()
    mobile_session.headers["User-Agent"] = MOODLE_MOBILE_USER_AGENT
    try:
        response = mobile_session.post(
            MOODLE_REST_URL,
            params={
                "moodlewsrestformat": "json",
                "wsfunction": "tool_mobile_get_autologin_key",
            },
            data={
                "wstoken": tokens.wstoken,
                "wsfunction": "tool_mobile_get_autologin_key",
                "privatetoken": tokens.private_token,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise BrowserBootstrapError(
            "could not request a Moodle browser auto-login key: "
            f"{safe_error_message(error)}"
        ) from None
    api_error = api_error_message(payload)
    if api_error is not None:
        if isinstance(payload, dict) and payload.get("errorcode") == (
            "autologinkeygenerationlockout"
        ):
            raise BrowserBootstrapError(
                "Moodle browser auto-login is rate-limited; retry in up to 6 minutes"
            )
        raise BrowserBootstrapError(f"Moodle browser auto-login failed: {api_error}")
    if not isinstance(payload, dict):
        raise BrowserBootstrapError("Moodle returned an unexpected auto-login response")
    key = payload.get("key")
    autologin_url = payload.get("autologinurl")
    if not isinstance(key, str) or not key or not isinstance(autologin_url, str):
        raise BrowserBootstrapError("Moodle returned an incomplete auto-login response")
    return _open_moodle_autologin(autologin_url, user_id, key)


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


def call_webservice(
    session: requests.Session,
    wstoken: str,
    function: str,
    data: dict[str, Any],
    log: logging.Logger = logger,
    *,
    warn_on_failure: bool = True,
) -> Any:
    request_data = {"wstoken": wstoken, "wsfunction": function, **data}
    try:
        response = session.post(
            MOODLE_REST_URL,
            params={"moodlewsrestformat": "json", "wsfunction": function},
            data=request_data,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        if warn_on_failure:
            log.warning(
                "Moodle web service %s failed: %s",
                function,
                safe_error_message(error),
            )
        return None
    api_error = api_error_message(payload)
    if api_error is not None:
        if warn_on_failure:
            log.warning("Moodle web service %s failed: %s", function, api_error)
        return None
    return payload


def _positive_id(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _changed_module_ids(instances: Any) -> frozenset[int] | None:
    if not isinstance(instances, list):
        return None
    changed: set[int] = set()
    for instance in instances:
        if not isinstance(instance, dict) or instance.get("contextlevel") != "module":
            return None
        module_id = _positive_id(instance.get("id"))
        updates = instance.get("updates")
        if module_id is None or not isinstance(updates, list):
            return None
        if any(
            not isinstance(update, dict)
            or not isinstance(update.get("name"), str)
            or not update["name"]
            for update in updates
        ):
            return None
        if updates:
            changed.add(module_id)
    return frozenset(changed)


def _unknown_module_ids(warnings: Any) -> frozenset[int] | None:
    if not isinstance(warnings, list):
        return None
    unknown: set[int] = set()
    for warning in warnings:
        if not isinstance(warning, dict) or warning.get("item") != "module":
            return None
        module_id = _positive_id(warning.get("itemid"))
        if module_id is None:
            return None
        unknown.add(module_id)
    return frozenset(unknown)


def get_course_updates_since(
    session: requests.Session,
    wstoken: str,
    course_id: int,
    since: int,
    log: logging.Logger = logger,
) -> CourseUpdates | None:
    """Return trustworthy module changes, or ``None`` when Moodle cannot tell."""
    payload = call_webservice(
        session,
        wstoken,
        MOODLE_UPDATE_FUNCTION,
        {"courseid": course_id, "since": since},
        log,
        warn_on_failure=False,
    )
    if not isinstance(payload, dict):
        return None
    changed = _changed_module_ids(payload.get("instances"))
    unknown = _unknown_module_ids(payload.get("warnings"))
    if changed is None or unknown is None:
        return None
    return CourseUpdates(since, changed, unknown)


def get_ltis_by_course(
    session: requests.Session, wstoken: str, course_id: int
) -> list[dict[str, Any]]:
    payload = call_webservice(
        session,
        wstoken,
        "mod_lti_get_ltis_by_courses",
        {"courseids[0]": course_id},
    )
    ltis = payload.get("ltis") if isinstance(payload, dict) else None
    return [item for item in ltis or [] if isinstance(item, dict)]


def get_lti_launch_data(
    session: requests.Session, wstoken: str, tool_id: int
) -> dict[str, Any] | None:
    payload = call_webservice(
        session, wstoken, "mod_lti_get_tool_launch_data", {"toolid": tool_id}
    )
    return payload if isinstance(payload, dict) else None


def get_h5pactivities_by_course(
    session: requests.Session, wstoken: str, course_id: int
) -> list[dict[str, Any]]:
    payload = call_webservice(
        session,
        wstoken,
        "mod_h5pactivity_get_h5pactivities_by_courses",
        {"courseids[0]": course_id},
    )
    activities = payload.get("h5pactivities") if isinstance(payload, dict) else None
    return [item for item in activities or [] if isinstance(item, dict)]


def get_quizzes_by_course(
    session: requests.Session, wstoken: str, course_id: int
) -> list[dict[str, Any]]:
    payload = call_webservice(
        session,
        wstoken,
        "mod_quiz_get_quizzes_by_courses",
        {"courseids[0]": course_id},
    )
    quizzes = payload.get("quizzes") if isinstance(payload, dict) else None
    return [item for item in quizzes or [] if isinstance(item, dict)]


def get_quiz_attempts(
    session: requests.Session, wstoken: str, quiz_id: int
) -> list[dict[str, Any]] | None:
    payload = call_webservice(
        session,
        wstoken,
        "mod_quiz_get_user_attempts",
        {"quizid": quiz_id, "status": "finished", "includepreviews": 0},
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("attempts"), list):
        return None
    return [item for item in payload["attempts"] if isinstance(item, dict)]


def get_quiz_attempt_review(
    session: requests.Session, wstoken: str, attempt_id: int
) -> dict[str, Any] | None:
    payload = call_webservice(
        session,
        wstoken,
        "mod_quiz_get_attempt_review",
        {"attemptid": attempt_id, "page": -1},
    )
    return payload if isinstance(payload, dict) else None


def _mobile_request_data(
    calls: list[tuple[str, dict[str, Any]]],
    *,
    filter_content: bool,
    rewrite_file_urls: bool,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for index, (function, arguments) in enumerate(calls):
        prefix = f"requests[{index}]"
        data[f"{prefix}[function]"] = function
        data[f"{prefix}[arguments]"] = json.dumps(arguments)
        data[f"{prefix}[settingfilter]"] = int(filter_content)
        data[f"{prefix}[settingfileurl]"] = int(rewrite_file_urls)
    return data


def _mobile_response_data(response: Any) -> tuple[Any, str | None]:
    if not isinstance(response, dict):
        return None, "unexpected response shape"
    if response.get("error"):
        return None, str(
            response.get("exception")
            or response.get("data")
            or "web service request failed"
        )
    try:
        return json.loads(response["data"]), None
    except (KeyError, TypeError, ValueError):
        return None, "unexpected response shape"


def get_all_courses(
    session: requests.Session,
    wstoken: str,
    user_id: Any,
    log: logging.Logger = logger,
) -> Any:
    payload = call_webservice(
        session,
        wstoken,
        "tool_mobile_call_external_functions",
        _mobile_request_data(
            [
                (
                    "core_enrol_get_users_courses",
                    {"userid": str(user_id), "returnusercount": "0"},
                )
            ],
            filter_content=True,
            rewrite_file_urls=True,
        ),
        log,
    )

    # Without the course list nothing can be synced, so API errors here are
    # fatal. A clear message beats the KeyError traceback this used to raise.
    error = "web service request failed" if payload is None else None
    if error is None:
        responses = payload.get("responses") if isinstance(payload, dict) else None
        first = responses[0] if isinstance(responses, list) and responses else None
        if not isinstance(first, dict):
            error = "unexpected response shape"
        else:
            courses, error = _mobile_response_data(first)
            if error is None and isinstance(courses, list):
                return courses
            if error is None:
                error = "unexpected response shape"
    log.critical(
        "Failed to retrieve the course list from Moodle: %s. Run "
        "`syncmymoodle auth status` to check the stored Moodle tokens, then "
        "`syncmymoodle auth login` if they need to be replaced.",
        error,
    )
    sys.exit(1)


def _direct_course_role_shortnames(payload: Any, user_id: Any) -> set[str] | None:
    if not isinstance(payload, list):
        return None
    profile = next(
        (
            item
            for item in payload
            if isinstance(item, dict) and str(item.get("id")) == str(user_id)
        ),
        None,
    )
    if profile is None or not isinstance((roles := profile.get("roles")), list):
        return None

    shortnames: set[str] = set()
    for role in roles:
        if (
            not isinstance(role, dict)
            or not isinstance((shortname := role.get("shortname")), str)
            or not shortname.strip()
        ):
            return None
        shortnames.add(shortname)
    return shortnames


def get_direct_course_roles_by_course(
    session: requests.Session,
    wstoken: str,
    user_id: Any,
    course_ids: list[Any],
    log: logging.Logger = logger,
) -> dict[str, set[str] | None]:
    """Return direct course-context role assignments in one mobile API call.

    Moodle's core profile API calls ``get_user_roles(..., false)`` and therefore
    does not expose assignments inherited from course categories or the system.
    """
    roles_by_course: dict[str, set[str] | None] = {
        str(course_id): None for course_id in course_ids
    }
    if not course_ids:
        return roles_by_course

    payload = call_webservice(
        session,
        wstoken,
        "tool_mobile_call_external_functions",
        _mobile_request_data(
            [
                (
                    "core_user_get_course_user_profiles",
                    {
                        "userlist": [
                            {"userid": str(user_id), "courseid": str(course_id)}
                        ]
                    },
                )
                for course_id in course_ids
            ],
            filter_content=False,
            rewrite_file_urls=False,
        ),
        log,
    )
    if payload is None:
        return roles_by_course
    responses = payload.get("responses") if isinstance(payload, dict) else None
    error = None
    if not isinstance(responses, list):
        responses = []
        error = "unexpected batch response shape"

    for course_id, response in zip(course_ids, responses, strict=False):
        response_payload, response_error = _mobile_response_data(response)
        if response_error is not None:
            error = response_error
            break
        roles = _direct_course_role_shortnames(response_payload, user_id)
        if roles is None:
            error = error or "profile roles were missing or malformed"
            continue
        roles_by_course[str(course_id)] = roles

    unknown_count = sum(roles is None for roles in roles_by_course.values())
    if unknown_count:
        if error is None:
            error = "the batch response ended early"
        log.warning(
            "Could not determine directly assigned Moodle course roles for %s "
            "course(s): %s; keeping those courses",
            unknown_count,
            error,
        )
    return roles_by_course


def get_course(
    session: requests.Session,
    wstoken: str,
    course_id: Any,
    log: logging.Logger = logger,
) -> list[Any] | None:
    payload = call_webservice(
        session,
        wstoken,
        "core_course_get_contents",
        {
            "courseid": int(course_id),
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
        },
        log,
    )
    # A course-specific error (hidden course, missing permission) should skip
    # this course but not abort the whole sync.
    if not isinstance(payload, list):
        log.error("Skipping course %s because Moodle returned no contents", course_id)
        return None
    return payload


def get_assignment(
    session: requests.Session,
    wstoken: str,
    course_id: Any,
    log: logging.Logger = logger,
) -> Any:
    payload = call_webservice(
        session,
        wstoken,
        "mod_assign_get_assignments",
        {
            "courseids[0]": int(course_id),
            "includenotenrolledcourses": 1,
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
        },
        log,
    )
    if not isinstance(payload, dict):
        return None
    courses = payload.get("courses") or []
    return courses[0] if courses else None


def get_assignment_submission_files(
    session: requests.Session,
    wstoken: str,
    user_id: Any,
    assignment_id: Any,
    log: logging.Logger = logger,
) -> list[Any] | None:
    payload = call_webservice(
        session,
        wstoken,
        "mod_assign_get_submission_status",
        {
            "assignid": assignment_id,
            "userid": user_id,
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
        },
        log,
    )
    if not isinstance(payload, dict):
        return None
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
    payload = call_webservice(
        session,
        wstoken,
        "mod_folder_get_folders_by_courses",
        {
            "courseids[0]": str(course_id),
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
        },
        log,
    )
    if not isinstance(payload, dict):
        return []
    return payload.get("folders") or []
