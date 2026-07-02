import logging
import sys

import requests
from bs4 import BeautifulSoup as bs

from syncmymoodle.constants import (
    MOODLE_URL,
    RWTH_DISRUPTIVE_STATUS_CLASSES,
    RWTH_HOMEPAGE_URL,
    RWTH_MOODLE_STATUS_URL,
    RWTH_SSO_STATUS_URL,
    RWTH_STATUS_URL,
)

logger = logging.getLogger(__name__)


def _tag_classes(tag):
    if tag is None:
        return set()
    classes = tag.get("class", [])
    if isinstance(classes, str):
        return {classes}
    return set(classes or [])


def check_general_connectivity(log: logging.Logger = logger):
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


def current_rwth_service_issues(service_name, status_url, log: logging.Logger = logger):
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

    soup = bs(response.text, features="lxml")
    issues = []
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


def check_rwth_status_page(log: logging.Logger = logger):
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


def check_moodle_availability(session, log: logging.Logger = logger):
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
