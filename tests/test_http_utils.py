import logging

import pytest
import requests

from syncmymoodle.http_utils import (
    HttpFailureKind,
    RequestPolicyError,
    ServiceOutageTracker,
    canonical_remote_url,
    classify_http_failure,
    classify_request_failure,
    moodle_url_allowed,
    normalized_http_origin,
    record_service_failure,
    redact_url_secrets,
    remote_request_scope_fingerprint,
    request_following_safe_redirects,
)

from .helpers import FakeResponse, FakeSession


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


@pytest.mark.parametrize(
    "url",
    [
        "http://moodle.rwth-aachen.de/webservice/rest/server.php",
        "https://moodle.rwth-aachen.de.evil.test/webservice/rest/server.php",
        "https://attacker@moodle.rwth-aachen.de/webservice/rest/server.php",
        " https://moodle.rwth-aachen.de/webservice/rest/server.php",
    ],
)
def test_moodle_credential_url_policy_rejects_unsafe_origins(url):
    assert not moodle_url_allowed(url)


def test_moodle_credential_url_policy_accepts_same_origin():
    assert moodle_url_allowed(
        "https://moodle.rwth-aachen.de/webservice/rest/server.php"
    )


def test_request_policy_failures_do_not_count_as_transient_outages():
    assert classify_request_failure(RequestPolicyError("blocked redirect")) is (
        HttpFailureKind.RESOURCE
    )
    assert classify_request_failure(requests.ConnectionError("offline")) is (
        HttpFailureKind.TRANSIENT
    )


def test_url_redaction_covers_userinfo_and_encoded_sensitive_names():
    value = (
        "failed https://alice:password@example.test/private"
        "?%74%6f%6b%65%6e=secret&safe=visible&OAuth_Signature=signed"
    )

    redacted = redact_url_secrets(value)

    assert "alice" not in redacted
    assert "password" not in redacted
    assert "secret" not in redacted
    assert "signed" not in redacted
    assert "safe=visible" in redacted
    assert redacted.count("[REDACTED]") == 3


def test_url_redaction_covers_aws_and_google_signed_query_credentials():
    value = (
        "https://cdn.example.test/video.mp4?"
        "X-Amz-Credential=aws-credential&X-Amz-Signature=aws-signature&"
        "X-Amz-Security-Token=aws-session-token&GoogleAccessId=google-id&"
        "X-Goog-Credential=google-credential&X-Goog-Signature=google-signature&"
        "X-Goog-Security-Token=google-session-token&X-Amz-Expires=300"
    )

    redacted = redact_url_secrets(value)

    for secret in (
        "aws-credential",
        "aws-signature",
        "aws-session-token",
        "google-id",
        "google-credential",
        "google-signature",
        "google-session-token",
    ):
        assert secret not in redacted
    assert redacted.count("[REDACTED]") == 7
    assert "X-Amz-Expires=300" in redacted


def test_canonical_remote_url_ignores_rotating_signed_fields_and_sorts_query():
    old = (
        "HTTPS://CDN.EXAMPLE.TEST/video.mp4?quality=hd&sig=azure-secret&"
        "se=2026-07-16T10%3A00Z&X-Amz-Date=20260716T100000Z&"
        "X-Amz-Credential=aws-secret#chapter"
    )
    new = (
        "https://cdn.example.test/video.mp4?X-Amz-Credential=new-aws-secret&"
        "X-Amz-Date=20260716T110000Z&se=2026-07-16T11%3A00Z&"
        "sig=new-azure-secret&quality=hd"
    )

    old_identity, old_display = canonical_remote_url(old)
    new_identity, new_display = canonical_remote_url(new)

    assert (
        old_identity
        == new_identity
        == ("https://cdn.example.test/video.mp4?quality=hd")
    )
    assert "azure-secret" not in old_display
    assert "aws-secret" not in old_display
    assert "new-azure-secret" not in new_display
    assert "new-aws-secret" not in new_display


def test_canonical_remote_url_does_not_display_fragment_secrets():
    identity, display = canonical_remote_url(
        "https://files.example.test/report.pdf#access_token=fragment-secret"
    )

    assert identity == "https://files.example.test/report.pdf"
    assert display == "https://files.example.test/report.pdf"
    assert "fragment-secret" not in display


def test_remote_request_scope_hashes_credentials_but_allows_signature_rotation():
    first_url = (
        "https://cdn.example.test/report.pdf?token=share-secret&"
        "X-Amz-Credential=account-scope&X-Amz-Date=20260716T100000Z&sig=first"
    )
    rotated_url = (
        "https://cdn.example.test/report.pdf?sig=second&"
        "X-Amz-Date=20260716T110000Z&X-Amz-Credential=account-scope&"
        "token=share-secret"
    )
    headers = {"Authorization": "Basic header-secret"}

    fingerprint = remote_request_scope_fingerprint(first_url, headers)

    assert fingerprint == remote_request_scope_fingerprint(rotated_url, headers)
    assert fingerprint != remote_request_scope_fingerprint(
        rotated_url,
        {"Authorization": "Basic other-secret"},
    )
    assert fingerprint != remote_request_scope_fingerprint(
        rotated_url.replace("share-secret", "other-share"),
        headers,
    )
    assert "secret" not in fingerprint
    assert "account-scope" not in fingerprint


def test_safe_redirect_changes_post_to_get_without_resending_body():
    session = FakeSession()
    start_url = "https://allowed.example.test/start"
    destination_url = "https://allowed.example.test/destination"
    session.add(
        "POST",
        start_url,
        FakeResponse(status_code=303, headers={"Location": destination_url}),
    )

    def destination(url, kwargs):
        del url
        assert "data" not in kwargs
        assert kwargs["headers"] == {"Accept": "text/html"}
        return FakeResponse(text="ok")

    session.add("GET", destination_url, destination)

    response = request_following_safe_redirects(
        session,
        "POST",
        start_url,
        lambda url: url.startswith("https://allowed.example.test/"),
        data={"password": "must-not-be-resent"},
        headers={"Accept": "text/html", "Content-Type": "application/x-form"},
        timeout=15,
    )

    assert response.text == "ok"
    assert session.calls == [("POST", start_url), ("GET", destination_url)]


def test_safe_redirect_preserves_webdav_method_and_body():
    session = FakeSession()
    start_url = "https://allowed.example.test/webdav"
    destination_url = f"{start_url}/"
    session.add(
        "PROPFIND",
        start_url,
        FakeResponse(status_code=301, headers={"Location": destination_url}),
    )

    def destination(url, kwargs):
        del url
        assert kwargs["data"] == "<propfind />"
        return FakeResponse(text="ok")

    session.add("PROPFIND", destination_url, destination)

    response = request_following_safe_redirects(
        session,
        "PROPFIND",
        start_url,
        lambda url: url.startswith("https://allowed.example.test/"),
        data="<propfind />",
        timeout=15,
    )

    assert response.text == "ok"
    assert session.calls == [
        ("PROPFIND", start_url),
        ("PROPFIND", destination_url),
    ]
