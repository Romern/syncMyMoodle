import logging
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from syncmymoodle.constants import (
    MOODLE_URL,
    RWTH_DISRUPTIVE_STATUS_CLASSES,
    RWTH_HOMEPAGE_URL,
    RWTH_MOODLE_STATUS_URL,
    RWTH_SSO_STATUS_URL,
    RWTH_STATUS_URL,
)
from syncmymoodle.context import SyncContext
from syncmymoodle.http_utils import get_input_value, parse_html
from syncmymoodle.storage import (
    load_cookies_from_data,
    read_private_gzip_json,
    save_session_cookies,
)
from syncmymoodle.totp import totp as generate_totp

logger = logging.getLogger(__name__)


def _tag_classes(tag: Any) -> set[str]:
    if tag is None:
        return set()
    classes = tag.get("class", [])
    if isinstance(classes, str):
        return {classes}
    return {str(class_name) for class_name in classes or []}


def _get_session_key(soup: Any, log: logging.Logger = logger) -> str:
    script = soup.find("script", string=lambda text: text and "sesskey" in text)
    match = re.search(r'"sesskey":"(.*?)"', script.text) if script is not None else None
    if match:
        return match.group(1)
    log.critical("Can't retrieve session key from JavaScript config")
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
            "Failed to login: expected form field %r was missing at the "
            "%s. The RWTH login flow may have changed or the servers may "
            "have difficulties. For current service status, see %s.",
            name,
            context,
            RWTH_STATUS_URL,
        )
        check_rwth_status_page(log)
        log.info("-------Login-Error-Soup--------")
        log.info(soup)
        sys.exit(1)
    return value


def check_general_connectivity(log: logging.Logger = logger) -> bool:
    try:
        response = requests.get(RWTH_HOMEPAGE_URL, timeout=10)
    except requests.RequestException as exc:
        log.warning(
            "General connectivity check to %s failed: %s",
            RWTH_HOMEPAGE_URL,
            exc,
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
        response = requests.get(status_url, timeout=10)
    except requests.RequestException as exc:
        log.warning(
            "Could not fetch RWTH ITC status page for %s: %s", service_name, exc
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
        ("RWTHmoodle", RWTH_MOODLE_STATUS_URL),
        ("RWTH Single Sign-On", RWTH_SSO_STATUS_URL),
    ]:
        issues.extend(current_rwth_service_issues(service_name, status_url, log))

    if not issues:
        log.info(
            "No current RWTHmoodle or RWTH Single Sign-On outage was found "
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
        response = session.get(MOODLE_URL, timeout=15)
    except requests.RequestException as exc:
        log.critical("Could not reach RWTHmoodle at %s: %s", MOODLE_URL, exc)
        check_general_connectivity(log)
        check_rwth_status_page(log)
        sys.exit(1)

    if response.status_code >= 500:
        log.critical(
            "RWTHmoodle returned status %s before login",
            response.status_code,
        )
        check_rwth_status_page(log)
        sys.exit(1)

    if response.status_code >= 400:
        log.warning(
            "RWTHmoodle availability check returned status %s; login may fail",
            response.status_code,
        )
        check_rwth_status_page(log)

    return response


def login(ctx: SyncContext, log: logging.Logger = logger) -> None:
    session = requests.Session()
    ctx.session = session
    cookie_file = Path(ctx.config.cookie_file).expanduser()
    cookie_payload = read_private_gzip_json(cookie_file, "session cookie")
    if cookie_payload is not None:
        load_cookies_from_data(session.cookies, cookie_payload)
    check_moodle_availability(session, log)
    try:
        resp = session.get(
            urllib.parse.urljoin(MOODLE_URL, "auth/shibboleth/index.php"),
            timeout=15,
        )
    except requests.RequestException as exc:
        log.critical("Could not reach RWTH SSO login endpoint: %s", exc)
        check_general_connectivity(log)
        check_rwth_status_page(log)
        sys.exit(1)
    if resp.url.startswith(f"{MOODLE_URL}my/"):
        soup = parse_html(resp.text)
        ctx.session_key = _get_session_key(soup, log)
        save_session_cookies(cookie_file, session.cookies)
        return

    # Create a separate soup for maintenance detection
    soup_check = parse_html(resp.text)

    # Remove known info banners by class
    for banner in soup_check.select(".themeboostunioninfobanner"):
        banner.decompose()

    # Also remove Bootstrap-style alert boxes marked as informational alerts
    for alert in soup_check.select('div.alert[role="alert"]'):
        alert.decompose()

    # Extract body text after cleanup
    body = soup_check.find("body")
    body_text = body.get_text(separator=" ", strip=True) if body else ""

    # Check for maintenance notice
    if "Wartungsarbeiten" in body_text:
        log.critical(
            "Detected Maintenance mode! If this is an error, please report it on GitHub."
        )
        log.info(f"Cleaned page body:\n{body_text}")
        sys.exit()

    soup = parse_html(resp.text)
    if soup.find("input", {"name": "RelayState"}) is None:
        csrf_token = _require_input_value(
            soup, "csrf_token", "username/password form", log
        )
        login_data = {
            "j_username": ctx.config.user,
            "j_password": ctx.config.password,
            "_eventId_proceed": "",
            "csrf_token": csrf_token,
        }
        resp2 = session.post(resp.url, data=login_data)

        soup = parse_html(resp2.text)

        if soup.find(id="fudis_selected_token_ids_input") is None:
            log.critical(
                "Failed to login. Maybe your login-info was wrong or the "
                "RWTH servers have difficulties. For current service "
                "status, see %s. For more info use the --verbose argument.",
                RWTH_STATUS_URL,
            )
            check_rwth_status_page(log)
            log.info("-------Login-Error-Soup--------")
            log.info(soup)
            sys.exit(1)

        csrf_token = _require_input_value(
            soup, "csrf_token", "TOTP generator selection form", log
        )

        print("Setting TOTP generator")
        totp_selection_data = {
            "fudis_selected_token_ids_input": ctx.config.totp,
            "_eventId_proceed": "",
            "csrf_token": csrf_token,
        }

        resp3 = session.post(resp2.url, data=totp_selection_data)

        soup = parse_html(resp3.text)
        if soup.find(id="fudis_otp_input") is None:
            log.critical(
                "Failed to select TOTP generator. Maybe your TOTP serial "
                "number is wrong or the RWTH servers have difficulties. "
                "For current service status, see %s. For more info use "
                "the --verbose argument.",
                RWTH_STATUS_URL,
            )
            check_rwth_status_page(log)
            log.info("-------Login-Error-Soup--------")
            log.info(soup)
            sys.exit(1)

        csrf_token = _require_input_value(soup, "csrf_token", "TOTP entry form", log)
        totp_secret = ctx.config.totpsecret
        if not totp_secret:
            totp_input = input(f"Enter TOTP for generator {ctx.config.totp}:\n")
        else:
            totp_input = generate_totp(totp_secret)
            print(f"Generated TOTP from provided secret: {totp_input}")

        totp_login_data = {
            "fudis_otp_input": totp_input,
            "_eventId_proceed": "",
            "csrf_token": csrf_token,
        }

        resp4 = session.post(resp3.url, data=totp_login_data)

        time.sleep(1)  # if we go too fast, we might have our connection closed
        soup = parse_html(resp4.text)
    if soup.find("input", {"name": "RelayState"}) is None:
        log.critical(
            "Failed to login. Maybe your login-info was wrong or the RWTH "
            "servers have difficulties. For current service status, see "
            "%s. For more info use the --verbose argument.",
            RWTH_STATUS_URL,
        )
        check_rwth_status_page(log)
        log.info("-------Login-Error-Soup--------")
        log.info(soup)
        sys.exit(1)
    data = {
        "RelayState": _require_input_value(soup, "RelayState", "SAML response", log),
        "SAMLResponse": _require_input_value(
            soup, "SAMLResponse", "SAML response", log
        ),
    }
    resp = session.post(f"{MOODLE_URL}Shibboleth.sso/SAML2/POST", data=data)
    soup = parse_html(resp.text)
    ctx.session_key = _get_session_key(soup, log)
    save_session_cookies(cookie_file, session.cookies)
