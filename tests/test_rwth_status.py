import logging

import pytest
import requests

from syncmymoodle import rwth
from syncmymoodle.constants import (
    HTTP_TIMEOUT_SECONDS,
    MOODLE_URL,
    RWTH_HOMEPAGE_URL,
    RWTH_MOODLE_STATUS_URL,
    RWTH_SSO_STATUS_URL,
)

from .helpers import FakeResponse, FakeSession

CURRENT_ISSUE_HTML = """
<div class="notification-card">
  <div class="notification-status-indicator"></div>
  <div class="incident_queue-statuses">
    <div class="statuslabel_stoerung">Disruption</div>
  </div>
  <div class="report_title"><h3>Moodle unavailable</h3></div>
  <span id="link-to-copy-1">https://status.example.test/issue/1</span>
</div>
<div class="notification-card">
  <div class="notification-status-indicator old"></div>
  <div class="incident_queue-statuses">
    <div class="statuslabel_stoerung">Resolved disruption</div>
  </div>
  <div class="report_title"><h3>Old incident</h3></div>
</div>
<div class="notification-card">
  <div class="notification-status-indicator"></div>
  <div class="incident_queue-statuses">
    <div class="statuslabel_betrieb">Operational</div>
  </div>
  <div class="report_title"><h3>Routine status</h3></div>
</div>
"""


def test_current_rwth_service_issues_selects_only_current_disruptions(monkeypatch):
    def get(url: str, *, timeout: int) -> FakeResponse:
        assert url == RWTH_MOODLE_STATUS_URL
        assert timeout == HTTP_TIMEOUT_SECONDS
        return FakeResponse(text=CURRENT_ISSUE_HTML)

    monkeypatch.setattr(rwth.requests, "get", get)

    assert rwth.current_rwth_service_issues("RWTH Moodle", RWTH_MOODLE_STATUS_URL) == [
        {
            "service": "RWTH Moodle",
            "status": "Disruption",
            "title": "Moodle unavailable",
            "url": "https://status.example.test/issue/1",
        }
    ]


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>maintenance proxy</body></html>",
        '<div class="notification-card"><div>incomplete card</div></div>',
        '<div class="notification-card"><div class="incident_queue-statuses"></div></div>',
    ],
)
def test_current_rwth_service_issues_ignores_malformed_html(monkeypatch, html):
    monkeypatch.setattr(
        rwth.requests,
        "get",
        lambda url, timeout: FakeResponse(text=html),
    )

    assert rwth.current_rwth_service_issues("RWTH Moodle", RWTH_MOODLE_STATUS_URL) == []


def test_current_rwth_service_issues_handles_non_2xx(monkeypatch, caplog):
    monkeypatch.setattr(
        rwth.requests,
        "get",
        lambda url, timeout: FakeResponse(status_code=503),
    )

    assert rwth.current_rwth_service_issues("RWTH Moodle", RWTH_MOODLE_STATUS_URL) == []
    assert "returned status 503" in caplog.text


def test_current_rwth_service_issues_handles_network_failure(monkeypatch, caplog):
    def fail(url: str, *, timeout: int) -> FakeResponse:
        del url, timeout
        raise requests.ConnectionError("status service offline")

    monkeypatch.setattr(rwth.requests, "get", fail)

    assert rwth.current_rwth_service_issues("RWTH Moodle", RWTH_MOODLE_STATUS_URL) == []
    assert "status service offline" in caplog.text


def test_check_rwth_status_page_reports_parsed_issue(monkeypatch, caplog):
    def get(url: str, *, timeout: int) -> FakeResponse:
        assert timeout == HTTP_TIMEOUT_SECONDS
        if url == RWTH_MOODLE_STATUS_URL:
            return FakeResponse(text=CURRENT_ISSUE_HTML)
        assert url == RWTH_SSO_STATUS_URL
        return FakeResponse(text="<html></html>")

    monkeypatch.setattr(rwth.requests, "get", get)
    caplog.set_level(logging.INFO, logger="syncmymoodle.rwth")

    rwth.check_rwth_status_page()

    assert "RWTH Moodle may currently be affected" in caplog.text
    assert "Moodle unavailable" in caplog.text
    assert "https://status.example.test/issue/1" in caplog.text


@pytest.mark.parametrize(("status_code", "expected"), [(200, True), (503, False)])
def test_general_connectivity_uses_bounded_request(
    monkeypatch,
    status_code: int,
    expected: bool,
):
    def get(url: str, *, timeout: int) -> FakeResponse:
        assert url == RWTH_HOMEPAGE_URL
        assert timeout == HTTP_TIMEOUT_SECONDS
        return FakeResponse(status_code=status_code)

    monkeypatch.setattr(rwth.requests, "get", get)

    assert rwth.check_general_connectivity() is expected


def test_general_connectivity_handles_network_failure(monkeypatch, caplog):
    def fail(url: str, *, timeout: int) -> FakeResponse:
        del url, timeout
        raise requests.Timeout("homepage timed out")

    monkeypatch.setattr(rwth.requests, "get", fail)

    assert rwth.check_general_connectivity() is False
    assert "homepage timed out" in caplog.text


def test_moodle_availability_warns_and_returns_4xx(monkeypatch, caplog):
    session = FakeSession()
    response = FakeResponse(status_code=404)
    session.add("GET", MOODLE_URL, response)
    status_checks = []
    monkeypatch.setattr(
        rwth,
        "check_rwth_status_page",
        lambda log: status_checks.append(log),
    )

    assert rwth.check_moodle_availability(session) is response
    assert len(status_checks) == 1
    assert "availability check returned status 404" in caplog.text


def test_moodle_availability_exits_on_5xx(monkeypatch, caplog):
    session = FakeSession()
    session.add("GET", MOODLE_URL, FakeResponse(status_code=503))
    status_checks = []
    monkeypatch.setattr(
        rwth,
        "check_rwth_status_page",
        lambda log: status_checks.append(log),
    )

    with pytest.raises(SystemExit) as error:
        rwth.check_moodle_availability(session)

    assert error.value.code == 1
    assert len(status_checks) == 1
    assert "returned status 503 before sign-in" in caplog.text


def test_moodle_availability_diagnoses_network_failure(monkeypatch, caplog):
    session = FakeSession()

    def fail(url: str, kwargs: dict[str, object]) -> FakeResponse:
        del url, kwargs
        raise requests.ConnectionError("Moodle offline")

    session.add("GET", MOODLE_URL, fail)
    diagnostics = []
    monkeypatch.setattr(
        rwth,
        "check_general_connectivity",
        lambda log: diagnostics.append("connectivity") or False,
    )
    monkeypatch.setattr(
        rwth,
        "check_rwth_status_page",
        lambda log: diagnostics.append("status"),
    )

    with pytest.raises(SystemExit) as error:
        rwth.check_moodle_availability(session)

    assert error.value.code == 1
    assert diagnostics == ["connectivity", "status"]
    assert "Moodle offline" in caplog.text
