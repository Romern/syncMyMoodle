import logging

import pytest
import requests

from syncmymoodle.http_utils import (
    HttpFailureKind,
    RequestPolicyError,
    ServiceOutageTracker,
    classify_http_failure,
    classify_request_failure,
    normalized_http_origin,
    record_service_failure,
)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (204, None),
        (404, HttpFailureKind.RESOURCE),
        (408, HttpFailureKind.TRANSIENT),
        (425, HttpFailureKind.TRANSIENT),
        (429, HttpFailureKind.TRANSIENT),
        (503, HttpFailureKind.TRANSIENT),
    ],
)
def test_http_failure_classification(status_code, expected):
    assert classify_http_failure(status_code) is expected


def test_service_outage_tracker_resets_on_availability_and_stays_open():
    tracker = ServiceOutageTracker()

    assert tracker.record_failure("service") is False
    tracker.record_available("service")
    assert tracker.record_failure("service") is False
    assert tracker.record_failure("service") is False
    assert tracker.record_failure("service") is True
    assert tracker.should_skip("service") is True

    tracker.record_available("service")
    assert tracker.should_skip("service") is True
    assert tracker.record_failure("service") is False


def test_resource_failure_resets_transient_service_failure_streak():
    tracker = ServiceOutageTracker()
    log = logging.getLogger("test.service-outage")

    for _ in range(2):
        record_service_failure(
            tracker,
            "service",
            "Service",
            HttpFailureKind.TRANSIENT,
            "temporarily unavailable",
            log,
        )
    record_service_failure(
        tracker,
        "service",
        "Service",
        HttpFailureKind.RESOURCE,
        "not found",
        log,
    )
    for _ in range(2):
        record_service_failure(
            tracker,
            "service",
            "Service",
            HttpFailureKind.TRANSIENT,
            "temporarily unavailable",
            log,
        )

    assert tracker.should_skip("service") is False


def test_http_origin_is_normalized_for_outage_keys():
    assert normalized_http_origin("HTTPS://Example.Test:443/path") == (
        "https://example.test"
    )
    assert normalized_http_origin("https://example.test:8443/path") == (
        "https://example.test:8443"
    )
    assert normalized_http_origin("not a URL") is None


def test_request_policy_failures_do_not_count_as_transient_outages():
    assert classify_request_failure(RequestPolicyError("blocked redirect")) is (
        HttpFailureKind.RESOURCE
    )
    assert classify_request_failure(requests.ConnectionError("offline")) is (
        HttpFailureKind.TRANSIENT
    )
