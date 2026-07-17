import logging
import sys
import time
import urllib.parse
from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

import requests

from syncmymoodle.constants import (
    HTTP_TIMEOUT_SECONDS,
    MOODLE_URL,
    RWTH_DISRUPTIVE_STATUS_CLASSES,
    RWTH_HOMEPAGE_URL,
    RWTH_MOODLE_STATUS_URL,
    RWTH_SSO_STATUS_URL,
    RWTH_STATUS_URL,
    RWTH_TOTP_MANAGER_URL,
)
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import (
    get_input_value,
    moodle_url_allowed,
    parse_html,
    request_following_safe_redirects,
    safe_error_message,
    same_origin,
    session_key_from_html,
)
from syncmymoodle.storage import (
    load_session_from_data,
    read_private_gzip_json,
    save_session,
)
from syncmymoodle.totp import totp as generate_totp

logger = logging.getLogger(__name__)
SESSION_REMAINING_URL = f"{MOODLE_URL}lib/ajax/service.php"
RWTH_SSO_ORIGINS = frozenset({"https://sso.rwth-aachen.de"})
SAML_RESPONSE_URL = f"{MOODLE_URL}Shibboleth.sso/SAML2/POST"


class SessionStatusKind(Enum):
    VALID = "valid"
    EXPIRED = "expired"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SessionStatus:
    kind: SessionStatusKind
    remaining_seconds: int | None = None
    detail: str | None = None


def _tag_classes(tag: Any) -> set[str]:
    if tag is None:
        return set()
    classes = tag.get("class", [])
    if isinstance(classes, str):
        return {classes}
    return {str(class_name) for class_name in classes or []}


def _get_session_key(soup: Any, log: logging.Logger = logger) -> str:
    session_key = session_key_from_html(str(soup))
    if session_key is not None:
        return session_key
    log.critical("Moodle did not provide a browser session key after sign-in.")
    sys.exit(1)


def _require_input_value(
    soup: Any,
    name: str,
    context: str,
    log: logging.Logger = logger,
) -> str:
    value = get_input_value(soup, name)
    if value is None:
        log.critical(
            "RWTH sign-in failed because the expected form field %r was "
            "missing from the %s. The sign-in page may have changed or the "
            "service may be unavailable. Check %s.",
            name,
            context,
            RWTH_STATUS_URL,
        )
        check_rwth_status_page(log)
        sys.exit(1)
    return value


def prompt_required_value(
    ctx: SyncContext,
    label: str,
    description: str,
    log: logging.Logger,
) -> str:
    value = ctx.output.prompt(label)
    if value:
        return value
    log.critical("A %s is required to log in.", description)
    sys.exit(1)


def ensure_login_credentials(ctx: SyncContext, log: logging.Logger) -> None:
    auth = ctx.auth
    if not auth.user:
        auth.user = prompt_required_value(ctx, "RWTH SSO username", "username", log)
    if auth.credential_resolver is not None:
        auth.credential_resolver()
    if not auth.password:
        auth.password = ctx.output.prompt_secret("RWTH SSO password")
    if not auth.password:
        log.critical("A password is required to log in.")
        sys.exit(1)


def ensure_totp_serial(ctx: SyncContext, log: logging.Logger) -> str:
    auth = ctx.auth
    if not auth.totp_serial:
        auth.totp_serial = prompt_required_value(
            ctx,
            "RWTH SSO TOTP serial id (for example, TOTP12345678)",
            "TOTP serial",
            log,
        )
    return auth.totp_serial


def check_general_connectivity(log: logging.Logger = logger) -> bool:
    try:
        response = requests.get(RWTH_HOMEPAGE_URL, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning(
            "General connectivity check to %s failed: %s",
            RWTH_HOMEPAGE_URL,
            safe_error_message(exc),
        )
        return False

    if response.status_code >= 500:
        log.warning(
            "General connectivity check to %s returned status %s",
            RWTH_HOMEPAGE_URL,
            response.status_code,
        )
        return False

    log.info("General connectivity check to %s succeeded", RWTH_HOMEPAGE_URL)
    return True


def current_rwth_service_issues(
    service_name: str,
    status_url: str,
    log: logging.Logger = logger,
) -> list[dict[str, str]]:
    try:
        response = requests.get(status_url, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning(
            "Could not fetch RWTH ITC status page for %s: %s",
            service_name,
            safe_error_message(exc),
        )
        return []

    if not (200 <= response.status_code < 300):
        log.warning(
            "RWTH ITC status page for %s returned status %s",
            service_name,
            response.status_code,
        )
        return []

    soup = parse_html(response.text)
    issues: list[dict[str, str]] = []
    for card in soup.select(".notification-card"):
        indicator = card.select_one(".notification-status-indicator")
        status_label = card.select_one(".incident_queue-statuses div")
        if "old" in _tag_classes(indicator):
            continue
        if "old" in _tag_classes(status_label):
            continue

        status_classes = _tag_classes(status_label)
        if not status_classes.intersection(RWTH_DISRUPTIVE_STATUS_CLASSES):
            continue

        title = card.select_one(".report_title h3")
        issue_link = card.select_one("[id^=link-to-copy-]")
        issues.append(
            {
                "service": service_name,
                "status": (
                    status_label.get_text(" ", strip=True)
                    if status_label
                    else "Status issue"
                ),
                "title": (
                    title.get_text(" ", strip=True)
                    if title
                    else "Current service issue"
                ),
                "url": (
                    issue_link.get_text(" ", strip=True) if issue_link else status_url
                ),
            }
        )
    return issues


def check_rwth_status_page(log: logging.Logger = logger) -> None:
    log.warning("Check the RWTH ITC status page: %s", RWTH_STATUS_URL)
    issues = []
    for service_name, status_url in [
        ("RWTH Moodle", RWTH_MOODLE_STATUS_URL),
        ("RWTH Single Sign-On", RWTH_SSO_STATUS_URL),
    ]:
        issues.extend(current_rwth_service_issues(service_name, status_url, log))

    if not issues:
        log.info(
            "No current RWTH Moodle or RWTH Single Sign-On outage was found "
            "on the RWTH ITC status pages"
        )
        return

    for issue in issues:
        log.warning(
            "%s may currently be affected: %s - %s. See %s",
            issue["service"],
            issue["status"],
            issue["title"],
            issue["url"],
        )


def check_moodle_availability(
    session: requests.Session | None, log: logging.Logger = logger
) -> requests.Response:
    if not session:
        raise Exception("You need a requests session first.")

    try:
        response = cast(
            requests.Response,
            request_following_safe_redirects(
                session,
                "GET",
                MOODLE_URL,
                moodle_url_allowed,
                timeout=HTTP_TIMEOUT_SECONDS,
            ),
        )
    except requests.RequestException as exc:
        log.critical(
            "Could not reach RWTH Moodle at %s: %s",
            MOODLE_URL,
            safe_error_message(exc),
        )
        check_general_connectivity(log)
        check_rwth_status_page(log)
        sys.exit(1)

    if response.status_code >= 500:
        log.critical(
            "RWTH Moodle returned status %s before sign-in",
            response.status_code,
        )
        check_rwth_status_page(log)
        sys.exit(1)

    if response.status_code >= 400:
        log.warning(
            "RWTH Moodle availability check returned status %s; sign-in may fail",
            response.status_code,
        )
        check_rwth_status_page(log)

    return response


def cached_session_status(cookie_file: Path) -> SessionStatus:
    """Check a cached Moodle session without refreshing its idle timeout."""
    payload = read_private_gzip_json(cookie_file, "cached browser session")
    if payload is None:
        return SessionStatus(SessionStatusKind.MISSING)

    session = requests.Session()
    session_key = load_session_from_data(session.cookies, payload)
    if session_key is None:
        return SessionStatus(
            SessionStatusKind.UNKNOWN,
            detail="legacy session cache; run `syncmymoodle auth login` once",
        )

    request = [{"index": 0, "methodname": "core_session_time_remaining", "args": {}}]
    try:
        response = cast(
            requests.Response,
            request_following_safe_redirects(
                session,
                "POST",
                SESSION_REMAINING_URL,
                moodle_url_allowed,
                params={
                    "sesskey": session_key,
                    "info": "core_session_time_remaining",
                },
                json=request,
                timeout=HTTP_TIMEOUT_SECONDS,
            ),
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        return SessionStatus(
            SessionStatusKind.UNKNOWN,
            detail=safe_error_message(error),
        )

    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return SessionStatus(SessionStatusKind.UNKNOWN, detail="unexpected response")
    result = payload[0]
    if result.get("error"):
        exception = result.get("exception")
        error_code = exception.get("errorcode") if isinstance(exception, dict) else None
        if error_code in {"invalidsesskey", "requireloginerror"}:
            return SessionStatus(SessionStatusKind.EXPIRED)
        detail = str(error_code) if error_code else "Moodle returned a status error"
        return SessionStatus(SessionStatusKind.UNKNOWN, detail=detail)
    data = result.get("data")
    remaining = data.get("timeremaining") if isinstance(data, dict) else None
    if not isinstance(remaining, int) or isinstance(remaining, bool):
        return SessionStatus(SessionStatusKind.UNKNOWN, detail="missing countdown")
    if remaining <= 0:
        return SessionStatus(SessionStatusKind.EXPIRED, remaining_seconds=0)
    return SessionStatus(SessionStatusKind.VALID, remaining_seconds=remaining)


def load_cached_session(cookie_file: Path) -> tuple[requests.Session, str] | None:
    payload = read_private_gzip_json(cookie_file, "cached browser session")
    if payload is None:
        return None
    session = requests.Session()
    session_key = load_session_from_data(session.cookies, payload)
    if session_key is None:
        return None
    return session, session_key


def _url_on_allowed_origin(url: str, allowed_origins: Collection[str]) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.username is None
        and parsed.password is None
        and any(same_origin(url, origin) for origin in allowed_origins)
    )


def sso_url_allowed(
    url: str,
    allowed_origins: Collection[str] = RWTH_SSO_ORIGINS,
) -> bool:
    """Return whether a credential-bearing SSO request may use this URL."""
    return _url_on_allowed_origin(url, allowed_origins)


def _login_url_allowed(url: str) -> bool:
    return moodle_url_allowed(url) or sso_url_allowed(url)


def _saml_response_url_allowed(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        expected = urllib.parse.urlsplit(SAML_RESPONSE_URL)
    except ValueError:
        return False
    return (
        moodle_url_allowed(url)
        and parsed.path == expected.path
        and not parsed.query
        and not parsed.fragment
    )


def _form_destination(
    soup: Any,
    field_name: str,
    base_url: str,
    default_url: str,
) -> str:
    field = soup.find("input", {"name": field_name})
    form = field.find_parent("form") if field is not None else None
    action = form.get("action") if form is not None else None
    return urllib.parse.urljoin(base_url, str(action)) if action else default_url


def post_sso_form(
    session: requests.Session,
    url: str,
    data: dict[str, Any],
    context: str,
    log: logging.Logger,
    url_allowed: Callable[[str], bool],
) -> requests.Response:
    try:
        return cast(
            requests.Response,
            request_following_safe_redirects(
                session,
                "POST",
                url,
                url_allowed,
                data=data,
                timeout=HTTP_TIMEOUT_SECONDS,
            ),
        )
    except requests.RequestException as error:
        log.critical(
            "Could not submit the RWTH SSO %s: %s",
            context,
            safe_error_message(error),
        )
        check_general_connectivity(log)
        check_rwth_status_page(log)
        sys.exit(1)


def _open_sso_login(
    session: requests.Session,
    log: logging.Logger,
) -> requests.Response:
    check_moodle_availability(session, log)
    try:
        return cast(
            requests.Response,
            request_following_safe_redirects(
                session,
                "GET",
                urllib.parse.urljoin(MOODLE_URL, "auth/shibboleth/index.php"),
                _login_url_allowed,
                timeout=HTTP_TIMEOUT_SECONDS,
            ),
        )
    except requests.RequestException as error:
        log.critical(
            "Could not reach RWTH SSO login endpoint: %s",
            safe_error_message(error),
        )
        check_general_connectivity(log)
        check_rwth_status_page(log)
        sys.exit(1)


def _check_for_maintenance(response: requests.Response, log: logging.Logger) -> None:
    soup = parse_html(response.text)
    for banner in soup.select(".themeboostunioninfobanner"):
        banner.decompose()
    for alert in soup.select('div.alert[role="alert"]'):
        alert.decompose()
    body = soup.find("body")
    body_text = body.get_text(separator=" ", strip=True) if body else ""
    if "Wartungsarbeiten" not in body_text:
        return
    log.critical(
        "Detected Maintenance mode! If this is an error, please report it on GitHub."
    )
    log.info("Cleaned page body:\n%s", body_text)
    sys.exit(1)


def _submit_password(
    ctx: SyncContext,
    session: requests.Session,
    response: requests.Response,
    soup: Any,
    log: logging.Logger,
) -> tuple[requests.Response, Any]:
    ensure_login_credentials(ctx, log)
    login_data = {
        "j_username": ctx.auth.user,
        "j_password": ctx.auth.password,
        "_eventId_proceed": "",
        "csrf_token": _require_input_value(
            soup, "csrf_token", "username/password form", log
        ),
    }
    response = post_sso_form(
        session,
        _form_destination(
            soup,
            "csrf_token",
            response.url,
            response.url,
        ),
        login_data,
        "username/password form",
        log,
        sso_url_allowed,
    )
    soup = parse_html(response.text)
    if soup.find(id="fudis_selected_token_ids_input") is not None:
        return response, soup
    log.critical(
        "RWTH rejected the username or password. Check them and try again. "
        "If they are correct, RWTH Single Sign-On may be unavailable; use "
        "--verbose for diagnostics."
    )
    check_rwth_status_page(log)
    sys.exit(1)


def _select_totp_method(
    ctx: SyncContext,
    session: requests.Session,
    response: requests.Response,
    soup: Any,
    log: logging.Logger,
) -> tuple[requests.Response, Any]:
    totp_serial = ensure_totp_serial(ctx, log)
    ctx.output.phase(f"Selecting TOTP method {totp_serial}...")
    selection_data = {
        "fudis_selected_token_ids_input": totp_serial,
        "_eventId_proceed": "",
        "csrf_token": _require_input_value(
            soup, "csrf_token", "TOTP method selection form", log
        ),
    }
    response = post_sso_form(
        session,
        _form_destination(
            soup,
            "csrf_token",
            response.url,
            response.url,
        ),
        selection_data,
        "TOTP method selection form",
        log,
        sso_url_allowed,
    )
    soup = parse_html(response.text)
    if soup.find(id="fudis_otp_input") is not None:
        return response, soup
    log.critical(
        "RWTH did not recognize TOTP serial %s. Check it in the RWTH IDM "
        "Token Manager at %s. If it is correct, RWTH Single Sign-On may be "
        "unavailable; use --verbose for diagnostics.",
        totp_serial,
        RWTH_TOTP_MANAGER_URL,
    )
    check_rwth_status_page(log)
    sys.exit(1)


def _current_totp_code(ctx: SyncContext) -> str | None:
    if ctx.auth.otp_code_resolver is not None:
        ctx.auth.otp_code = ctx.auth.otp_code_resolver()
    if ctx.auth.otp_code:
        return ctx.auth.otp_code
    if not ctx.auth.totp_secret:
        return ctx.output.prompt(
            f"Current 6-digit TOTP code for {ctx.auth.totp_serial}"
        )
    code = generate_totp(ctx.auth.totp_secret)
    ctx.output.print("Generated the current TOTP code from the configured seed.")
    return code


def _submit_totp(
    ctx: SyncContext,
    session: requests.Session,
    response: requests.Response,
    soup: Any,
    log: logging.Logger,
) -> Any:
    login_data = {
        "fudis_otp_input": _current_totp_code(ctx),
        "_eventId_proceed": "",
        "csrf_token": _require_input_value(soup, "csrf_token", "TOTP entry form", log),
    }
    response = post_sso_form(
        session,
        _form_destination(
            soup,
            "csrf_token",
            response.url,
            response.url,
        ),
        login_data,
        "TOTP entry form",
        log,
        sso_url_allowed,
    )
    time.sleep(1)  # RWTH may close a connection advanced too quickly.
    return parse_html(response.text)


def _saml_form(
    ctx: SyncContext,
    session: requests.Session,
    response: requests.Response,
    log: logging.Logger,
) -> Any:
    soup = parse_html(response.text)
    if soup.find("input", {"name": "RelayState"}) is not None:
        return soup
    response, soup = _submit_password(ctx, session, response, soup, log)
    response, soup = _select_totp_method(ctx, session, response, soup, log)
    return _submit_totp(ctx, session, response, soup, log)


def _submit_saml_response(
    session: requests.Session,
    soup: Any,
    log: logging.Logger,
) -> str:
    if soup.find("input", {"name": "RelayState"}) is None:
        log.critical(
            "RWTH sign-in failed after TOTP verification. The code may be "
            "incorrect or expired; try again. If the problem continues, RWTH "
            "Single Sign-On may be unavailable; use --verbose for diagnostics."
        )
        check_rwth_status_page(log)
        sys.exit(1)
    data = {
        "RelayState": _require_input_value(soup, "RelayState", "SAML response", log),
        "SAMLResponse": _require_input_value(
            soup, "SAMLResponse", "SAML response", log
        ),
    }
    destination = _form_destination(
        soup,
        "RelayState",
        MOODLE_URL,
        SAML_RESPONSE_URL,
    )
    if not _saml_response_url_allowed(destination):
        log.critical("RWTH sign-in returned an unexpected SAML response destination.")
        sys.exit(1)
    response = post_sso_form(
        session,
        destination,
        data,
        "SAML response",
        log,
        moodle_url_allowed,
    )
    return _get_session_key(parse_html(response.text), log)


def _finish_login(
    ctx: SyncContext,
    session: requests.Session,
    cookie_file: Path,
    session_key: str,
    persist_session: bool,
) -> None:
    ctx.session_key = session_key
    if persist_session and not ctx.config.dry_run:
        save_session(cookie_file, session.cookies, session_key)


def login(
    ctx: SyncContext,
    log: logging.Logger = logger,
    *,
    reuse_cached_session: bool = True,
    persist_session: bool = True,
) -> None:
    session = requests.Session()
    ctx.session = session
    cookie_file = Path(ctx.config.cookie_file).expanduser()
    if reuse_cached_session:
        cookie_payload = read_private_gzip_json(cookie_file, "cached browser session")
        if cookie_payload is not None:
            load_session_from_data(session.cookies, cookie_payload)
    response = _open_sso_login(session, log)
    if response.url.startswith(f"{MOODLE_URL}my/"):
        _finish_login(
            ctx,
            session,
            cookie_file,
            _get_session_key(parse_html(response.text), log),
            persist_session,
        )
        return
    _check_for_maintenance(response, log)
    session_key = _submit_saml_response(
        session,
        _saml_form(ctx, session, response, log),
        log,
    )
    _finish_login(ctx, session, cookie_file, session_key, persist_session)
