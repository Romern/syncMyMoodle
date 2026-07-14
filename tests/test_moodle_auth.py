import base64
import hashlib
from datetime import UTC, datetime

import pytest
import requests

from syncmymoodle import moodle
from syncmymoodle.constants import MOODLE_URL
from syncmymoodle.http_utils import request_following_safe_redirects, safe_request_error
from syncmymoodle.moodle_tokens import MoodleTokens

from .helpers import FakeResponse, FakeSession


def launch_location(passport, wstoken="ws-token", private_token="private-token"):
    server_site = MOODLE_URL.rstrip("/")
    signature = hashlib.md5(
        f"{server_site}{passport}".encode(), usedforsecurity=False
    ).hexdigest()
    parts = [signature, wstoken]
    if private_token is not None:
        parts.append(private_token)
    encoded = base64.b64encode(":::".join(parts).encode()).decode()
    return f"syncmymoodle://token={encoded}"


def test_mobile_launch_parser_returns_paired_tokens():
    tokens = moodle.parse_mobile_launch_location(
        launch_location("passport"),
        "passport",
        "ab123456",
    )

    assert tokens == MoodleTokens(
        username="ab123456",
        wstoken="ws-token",
        private_token="private-token",
    )


def test_mobile_launch_parser_accepts_legacy_missing_private_token():
    tokens = moodle.parse_mobile_launch_location(
        launch_location("passport", private_token=None),
        "passport",
        "ab123456",
    )

    assert tokens.private_token is None


def test_mobile_launch_parser_rejects_response_for_another_passport():
    try:
        moodle.parse_mobile_launch_location(
            launch_location("other-passport"),
            "expected-passport",
            "ab123456",
        )
    except moodle.MobileLaunchError as error:
        assert "correlation" in str(error)
    else:
        raise AssertionError("mismatched passport was accepted")


def test_acquire_mobile_tokens_uses_random_correlated_passport():
    session = FakeSession()
    session.add(
        "GET",
        MOODLE_URL,
        FakeResponse(text='<script>{"userId":"123"}</script>'),
    )

    def launch_response(url, kwargs):
        assert kwargs["allow_redirects"] is False
        assert kwargs["params"]["service"] == "moodle_mobile_app"
        assert kwargs["params"]["urlscheme"] == "syncmymoodle"
        passport = kwargs["params"]["passport"]
        return FakeResponse(headers={"Location": launch_location(passport)})

    session.add("GET", moodle.MOODLE_MOBILE_LAUNCH_URL, launch_response)

    tokens = moodle.acquire_mobile_tokens(session, "ab123456")

    assert tokens.wstoken == "ws-token"
    assert tokens.private_token == "private-token"
    assert tokens.moodle_user_id == 123


def test_acquire_mobile_tokens_requires_browser_account_identity():
    session = FakeSession()
    session.add("GET", MOODLE_URL, FakeResponse(text="<html></html>"))

    with pytest.raises(moodle.MobileLaunchError, match="valid user id"):
        moodle.acquire_mobile_tokens(session, "ab123456")

    assert session.count("GET", moodle.MOODLE_MOBILE_LAUNCH_URL) == 0


def validation_session(payload, status_code=200, headers=None):
    session = FakeSession()
    session.add(
        "POST",
        moodle.MOODLE_REST_URL,
        FakeResponse(
            status_code=status_code, json_payload=payload, headers=headers or {}
        ),
    )
    return session


def bound_tokens(user_id=123):
    return MoodleTokens(
        "ab123456",
        "ws-token",
        "private-token",
        moodle_user_id=user_id,
    )


def test_token_validation_returns_site_info_for_matching_account():
    payload = {
        "userid": 123,
        "username": "https://sso.example.test/idp!opaque-persistent-id",
        "siteurl": MOODLE_URL.rstrip("/"),
        "userprivateaccesskey": "download-key",
    }

    result = moodle.validate_mobile_tokens(
        bound_tokens(),
        session=validation_session(payload),
    )

    assert result.kind is moodle.TokenValidationKind.VALID
    assert result.site_info == payload


def test_token_validation_records_moodle_server_time():
    payload = {
        "userid": 123,
        "username": "opaque-id",
        "siteurl": MOODLE_URL.rstrip("/"),
    }

    result = moodle.validate_mobile_tokens(
        bound_tokens(),
        session=validation_session(
            payload,
            headers={"Date": "Tue, 14 Jul 2026 12:00:00 GMT"},
        ),
    )

    assert result.server_time == int(datetime(2026, 7, 14, 12, tzinfo=UTC).timestamp())


def test_token_validation_distinguishes_revocation_from_server_failure():
    invalid = moodle.validate_mobile_tokens(
        bound_tokens(),
        session=validation_session(
            {
                "exception": "moodle_exception",
                "errorcode": "invalidtoken",
                "message": "Invalid token",
            }
        ),
    )
    unavailable = moodle.validate_mobile_tokens(
        bound_tokens(),
        session=validation_session(
            {"exception": "moodle_exception", "message": "Oops"}
        ),
    )

    assert invalid.kind is moodle.TokenValidationKind.INVALID
    assert unavailable.kind is moodle.TokenValidationKind.UNKNOWN


def test_token_validation_treats_transient_invalidtoken_response_as_unknown():
    result = moodle.validate_mobile_tokens(
        bound_tokens(),
        session=validation_session(
            {
                "exception": "moodle_exception",
                "errorcode": "invalidtoken",
                "message": "Invalid token",
            },
            status_code=503,
        ),
    )

    assert result.kind is moodle.TokenValidationKind.UNKNOWN
    assert result.detail == "Moodle token validation returned HTTP 503"


def test_token_validation_does_not_treat_access_errors_as_revocation():
    result = moodle.validate_mobile_tokens(
        bound_tokens(),
        session=validation_session(
            {
                "exception": "required_capability_exception",
                "errorcode": "accessexception",
                "message": "Access denied",
            }
        ),
    )

    assert result.kind is moodle.TokenValidationKind.UNKNOWN


def test_token_validation_requires_site_identity():
    result = moodle.validate_mobile_tokens(
        bound_tokens(),
        session=validation_session({"userid": 123, "username": "ab123456"}),
    )

    assert result.kind is moodle.TokenValidationKind.UNKNOWN
    assert "site URL" in (result.detail or "")


def test_token_validation_requires_integer_user_id():
    result = moodle.validate_mobile_tokens(
        bound_tokens(),
        session=validation_session(
            {"userid": "123", "username": "ab123456", "siteurl": MOODLE_URL}
        ),
    )

    assert result.kind is moodle.TokenValidationKind.UNKNOWN
    assert "valid user id" in (result.detail or "")


def test_token_validation_treats_network_failure_as_unknown():
    class FailingSession:
        def post(self, *args, **kwargs):
            raise requests.ConnectionError("offline")

    result = moodle.validate_mobile_tokens(
        bound_tokens(),
        session=FailingSession(),
    )

    assert result.kind is moodle.TokenValidationKind.UNKNOWN


def test_token_validation_rejects_token_for_another_account():
    result = moodle.validate_mobile_tokens(
        bound_tokens(),
        session=validation_session(
            {
                "userid": 456,
                "username": "https://sso.example.test/idp!another-opaque-id",
                "siteurl": MOODLE_URL,
            }
        ),
    )

    assert result.kind is moodle.TokenValidationKind.INVALID
    assert "another Moodle account" in (result.detail or "")


def test_token_validation_rejects_unbound_token_record():
    result = moodle.validate_mobile_tokens(
        MoodleTokens("ab123456", "ws-token", "private-token"),
        session=validation_session(
            {
                "userid": 123,
                "username": "https://sso.example.test/idp!opaque-id",
                "siteurl": MOODLE_URL,
            }
        ),
    )

    assert result.kind is moodle.TokenValidationKind.INVALID
    assert "account identity" in (result.detail or "")


def test_security_keys_parser_selects_only_mobile_service_token():
    html = """
    <table>
      <tr><td>Other service</td><td><a href="?action=resetwstoken&amp;tokenid=41">Reset</a></td></tr>
      <tr><td>Moodle mobile web service</td><td><a href="?action=resetwstoken&amp;tokenid=42">Reset</a></td></tr>
    </table>
    """

    assert moodle.mobile_token_id_from_security_keys(html) == "42"


def test_reset_mobile_token_uses_confirmed_sesskey_request():
    session = FakeSession()
    session.add(
        "GET",
        moodle.MOODLE_MANAGE_TOKEN_URL,
        FakeResponse(
            text=(
                "<table><tr><td>Moodle mobile web service</td><td>"
                '<a href="?action=resetwstoken&amp;tokenid=42">Reset</a>'
                "</td></tr></table>"
            )
        ),
    )

    def reset_response(url, kwargs):
        assert kwargs["params"] == {
            "action": "resetwstoken",
            "tokenid": "42",
            "confirm": "1",
            "sesskey": "session-key",
        }
        return FakeResponse()

    session.routes[("GET", moodle.MOODLE_MANAGE_TOKEN_URL)] = lambda url, kwargs: (
        reset_response(url, kwargs)
        if "params" in kwargs
        else FakeResponse(
            text=(
                "<table><tr><td>Moodle mobile web service</td><td>"
                '<a href="?action=resetwstoken&amp;tokenid=42">Reset</a>'
                "</td></tr></table>"
            )
        )
    )

    moodle.reset_mobile_token(session, "session-key")

    assert session.count("GET", moodle.MOODLE_MANAGE_TOKEN_URL) == 2


def test_token_session_rewrites_only_same_site_moodle_file_urls():
    auth = moodle.MoodleTokenAuth("secret-token")
    moodle_request = requests.Request(
        "GET",
        f"{MOODLE_URL}pluginfile.php/42/mod_resource/content/1/file.pdf?forced=1",
    ).prepare()
    external_request = requests.Request(
        "GET", "https://files.example.test/pluginfile.php/file.pdf"
    ).prepare()

    auth(moodle_request)
    auth(external_request)

    assert moodle_request.url == (
        f"{MOODLE_URL}webservice/pluginfile.php/42/mod_resource/content/1/file.pdf"
        "?forced=1&token=secret-token"
    )
    assert external_request.url == "https://files.example.test/pluginfile.php/file.pdf"


def test_token_session_uses_user_private_key_for_tokenpluginfile_urls():
    request = requests.Request(
        "GET",
        f"{MOODLE_URL}tokenpluginfile.php/42/question/questiontext/file.png",
    ).prepare()

    moodle.MoodleTokenAuth("ws-token", "download-key")(request)

    assert request.url == (
        f"{MOODLE_URL}tokenpluginfile.php/42/question/questiontext/file.png"
        "?token=download-key"
    )


def test_token_session_replaces_stale_cached_file_tokens():
    request = requests.Request(
        "GET",
        (
            f"{MOODLE_URL}webservice/pluginfile.php/42/mod_resource/content/1/file.pdf"
            "?forced=1&token=old-token"
        ),
    ).prepare()

    moodle.MoodleTokenAuth("current-token")(request)

    assert request.url == (
        f"{MOODLE_URL}webservice/pluginfile.php/42/mod_resource/content/1/file.pdf"
        "?forced=1&token=current-token"
    )


def test_token_session_does_not_use_wstoken_as_tokenpluginfile_key():
    url = f"{MOODLE_URL}tokenpluginfile.php/42/question/questiontext/file.png"
    request = requests.Request("GET", url).prepare()

    moodle.MoodleTokenAuth("ws-token")(request)

    assert request.url == url


def test_request_error_redacts_browser_query_credentials():
    request = requests.Request(
        "GET",
        f"{MOODLE_URL}login?key=one-use-key&sesskey=browser-session-key",
    ).prepare()
    error = requests.ConnectionError(
        f"request failed for {request.url}",
        request=request,
    )

    message = safe_request_error(error)

    assert "one-use-key" not in message
    assert "browser-session-key" not in message
    assert message.count("[REDACTED]") == 2


def test_safe_request_response_url_omits_auth_injected_token():
    url = f"{MOODLE_URL}pluginfile.php/42/mod_resource/content/1/file.pdf"
    session = moodle.create_token_session(bound_tokens())
    requested_urls = []

    class SuccessfulAdapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            requested_urls.append(request.url)
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            return response

        def close(self):
            pass

    session.mount("https://", SuccessfulAdapter())

    response = request_following_safe_redirects(
        session,
        "HEAD",
        url,
        lambda candidate: candidate == url,
        timeout=15,
    )

    assert requested_urls == [
        f"{MOODLE_URL}webservice/pluginfile.php/42/mod_resource/content/1/file.pdf"
        "?token=ws-token"
    ]
    assert response.url == url


def test_create_browser_session_uses_private_token_and_mobile_user_agent(monkeypatch):
    mobile_session = FakeSession()
    browser_session = FakeSession()

    def key_response(url, kwargs):
        assert mobile_session.headers["User-Agent"].startswith("MoodleMobile")
        assert kwargs["data"] == {
            "wstoken": "ws-token",
            "wsfunction": "tool_mobile_get_autologin_key",
            "privatetoken": "private-token",
        }
        return FakeResponse(
            json_payload={
                "key": "one-use-key",
                "autologinurl": f"{MOODLE_URL}admin/tool/mobile/autologin.php",
            }
        )

    mobile_session.add("POST", moodle.MOODLE_REST_URL, key_response)

    def login_response(url, kwargs):
        assert kwargs["params"] == {"userid": "123", "key": "one-use-key"}
        return FakeResponse(text='<script>{"sesskey":"browser-sesskey"}</script>')

    browser_session.add(
        "GET", f"{MOODLE_URL}admin/tool/mobile/autologin.php", login_response
    )
    sessions = iter([mobile_session, browser_session])
    monkeypatch.setattr(moodle.requests, "Session", lambda: next(sessions))

    returned_session, session_key = moodle.create_browser_session(bound_tokens())

    assert returned_session is browser_session
    assert session_key == "browser-sesskey"


def test_create_browser_session_names_missing_browser_login_token():
    tokens = MoodleTokens("user", "ws-token", None, moodle_user_id=123)

    with pytest.raises(moodle.BrowserBootstrapError) as exc_info:
        moodle.create_browser_session(tokens)

    assert "browser login token" in str(exc_info.value)
    assert "private token" not in str(exc_info.value)


def test_create_browser_session_rejects_external_autologin_url(monkeypatch):
    mobile_session = FakeSession()
    mobile_session.add(
        "POST",
        moodle.MOODLE_REST_URL,
        FakeResponse(
            json_payload={
                "key": "one-use-key",
                "autologinurl": "https://example.test/steal-key",
            }
        ),
    )
    monkeypatch.setattr(moodle.requests, "Session", lambda: mobile_session)

    with pytest.raises(moodle.BrowserBootstrapError, match="unsafe auto-login URL"):
        moodle.create_browser_session(bound_tokens())

    assert mobile_session.calls == [("POST", moodle.MOODLE_REST_URL)]


def test_create_browser_session_reports_shared_rate_limit(monkeypatch):
    mobile_session = FakeSession()
    mobile_session.add(
        "POST",
        moodle.MOODLE_REST_URL,
        FakeResponse(
            json_payload={
                "exception": "moodle_exception",
                "errorcode": "autologinkeygenerationlockout",
                "message": "Please try later",
            }
        ),
    )
    monkeypatch.setattr(moodle.requests, "Session", lambda: mobile_session)

    with pytest.raises(moodle.BrowserBootstrapError, match="up to 6 minutes"):
        moodle.create_browser_session(bound_tokens())
