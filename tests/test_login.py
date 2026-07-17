import logging

import pytest
import requests

import syncmymoodle.rwth as rwth
from syncmymoodle.constants import MOODLE_URL
from syncmymoodle.context import MoodleAccount
from syncmymoodle.moodle_tokens import MoodleTokens

from .helpers import FakeResponse, FakeSession, make_context

SSO_ORIGIN = "https://sso.rwth-aachen.de"


def fresh_login_session():
    session = FakeSession()
    session.cookies = []
    login_url = f"{SSO_ORIGIN}/login"
    select_url = f"{SSO_ORIGIN}/select-token"
    otp_url = f"{SSO_ORIGIN}/otp"
    posted_login = []
    posted_totp_serial = []
    posted_otp = []

    session.add(
        "GET",
        f"{MOODLE_URL}auth/shibboleth/index.php",
        FakeResponse(status_code=302, headers={"Location": login_url}),
    )
    session.add(
        "GET",
        login_url,
        FakeResponse(
            text="""
<form action="/login">
<input name="csrf_token" value="csrf-login">
</form>
"""
        ),
    )

    def login_response(url, kwargs):
        del url
        posted_login.append(kwargs["data"])
        return FakeResponse(
            status_code=303,
            headers={"Location": select_url},
        )

    session.add("POST", login_url, login_response)
    session.add(
        "GET",
        select_url,
        FakeResponse(
            text="""
<form action="/select-token">
<input id="fudis_selected_token_ids_input">
<input name="csrf_token" value="csrf-select">
</form>
"""
        ),
    )

    def select_response(url, kwargs):
        del url
        posted_totp_serial.append(kwargs["data"]["fudis_selected_token_ids_input"])
        return FakeResponse(status_code=303, headers={"Location": otp_url})

    session.add("POST", select_url, select_response)
    session.add(
        "GET",
        otp_url,
        FakeResponse(
            text="""
<form action="/otp">
<input id="fudis_otp_input">
<input name="csrf_token" value="csrf-otp">
</form>
"""
        ),
    )

    def otp_response(url, kwargs):
        del url
        posted_otp.append(kwargs["data"]["fudis_otp_input"])
        return FakeResponse(
            text="""
<form action="https://moodle.rwth-aachen.de/Shibboleth.sso/SAML2/POST">
<input name="RelayState" value="relay">
<input name="SAMLResponse" value="saml">
</form>
"""
        )

    session.add("POST", otp_url, otp_response)
    session.add(
        "POST",
        rwth.SAML_RESPONSE_URL,
        FakeResponse(status_code=302, headers={"Location": f"{MOODLE_URL}my/"}),
    )
    session.add(
        "GET",
        f"{MOODLE_URL}my/",
        FakeResponse(text='<script>{"sesskey":"abc123"}</script>'),
    )
    return session, posted_login, posted_totp_serial, posted_otp


@pytest.fixture
def no_login_delay(monkeypatch):
    monkeypatch.setattr(rwth.time, "sleep", lambda seconds: None)


def test_sync_context_repr_redacts_runtime_secrets():
    ctx = make_context()
    ctx.session_key = "browser-session-secret"
    ctx.moodle_account = MoodleAccount(
        MoodleTokens(
            "user",
            "mobile-webservice-secret",
            "private-secret",
            moodle_user_id=123,
        ),
    )

    representation = repr(ctx)

    assert "browser-session-secret" not in representation
    assert "mobile-webservice-secret" not in representation
    assert "private-secret" not in representation


@pytest.mark.parametrize(("dry_run", "expected_saves"), [(True, 0), (False, 1)])
def test_login_only_saves_session_cookies_outside_dry_run(
    tmp_path,
    monkeypatch,
    dry_run,
    expected_saves,
):
    session = FakeSession()
    session.cookies = []
    session.add(
        "GET",
        f"{MOODLE_URL}auth/shibboleth/index.php",
        FakeResponse(status_code=302, headers={"Location": f"{MOODLE_URL}my/"}),
    )
    session.add(
        "GET",
        f"{MOODLE_URL}my/",
        FakeResponse(text='<script>{"sesskey":"abc123"}</script>'),
    )
    saved_sessions = []
    ctx = make_context(
        {
            "downloads.dry_run": dry_run,
            "paths.cookie_file": str(tmp_path / "session"),
        }
    )
    ctx.auth.credential_resolver = lambda: pytest.fail("unexpected resolver call")
    ctx.auth.otp_code_resolver = lambda: pytest.fail("unexpected OTP resolver call")

    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)
    monkeypatch.setattr(
        rwth,
        "save_session",
        lambda path, cookies, session_key: saved_sessions.append((path, session_key)),
    )

    rwth.login(ctx)

    assert ctx.session_key == "abc123"
    assert saved_sessions == (
        [(tmp_path / "session", "abc123")] if expected_saves else []
    )


def test_login_maintenance_notice_exits_with_failure(monkeypatch):
    session = FakeSession()
    session.add(
        "GET",
        f"{MOODLE_URL}auth/shibboleth/index.php",
        FakeResponse(
            text="<html><body>Wartungsarbeiten</body></html>",
            url=f"{MOODLE_URL}maintenance",
        ),
    )
    ctx = make_context()
    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)

    with pytest.raises(SystemExit) as exc_info:
        rwth.login(ctx, reuse_cached_session=False)

    assert exc_info.value.code == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://sso.rwth-aachen.de/login",
        "https://sso.rwth-aachen.de.evil.test/login",
        "https://attacker@sso.rwth-aachen.de/login",
        "https://evil.test/login",
    ],
)
def test_sso_url_allowlist_rejects_non_official_destinations(url):
    assert not rwth.sso_url_allowed(url)


def test_sso_url_allowlist_accepts_official_and_injected_test_origins():
    assert rwth.sso_url_allowed(f"{SSO_ORIGIN}/idp/login")
    assert rwth.sso_url_allowed(
        "https://test-idp.example/login",
        {"https://test-idp.example"},
    )


def test_login_refuses_non_sso_redirect_before_requesting_it(monkeypatch):
    session = FakeSession()
    login_url = f"{MOODLE_URL}auth/shibboleth/index.php"
    session.add(
        "GET",
        login_url,
        FakeResponse(
            status_code=302,
            headers={"Location": "https://evil.test/credential-capture"},
        ),
    )
    ctx = make_context()
    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)
    monkeypatch.setattr(rwth, "check_general_connectivity", lambda log: None)
    monkeypatch.setattr(rwth, "check_rwth_status_page", lambda log: None)

    with pytest.raises(SystemExit):
        rwth.login(ctx, reuse_cached_session=False)

    assert session.calls == [("GET", login_url)]


def test_login_refuses_cross_origin_form_action_before_posting(monkeypatch):
    session = FakeSession()
    login_url = f"{SSO_ORIGIN}/login"
    entry_url = f"{MOODLE_URL}auth/shibboleth/index.php"
    session.add(
        "GET",
        entry_url,
        FakeResponse(status_code=302, headers={"Location": login_url}),
    )
    session.add(
        "GET",
        login_url,
        FakeResponse(
            text="""
<form action="https://evil.test/credential-capture">
<input name="csrf_token" value="csrf-login">
</form>
"""
        ),
    )
    ctx = make_context({"auth.user": "user"})
    ctx.auth.password = "password"
    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)
    monkeypatch.setattr(rwth, "check_general_connectivity", lambda log: None)
    monkeypatch.setattr(rwth, "check_rwth_status_page", lambda log: None)

    with pytest.raises(SystemExit):
        rwth.login(ctx, reuse_cached_session=False)

    assert session.calls == [("GET", entry_url), ("GET", login_url)]


def test_sso_post_does_not_resend_credentials_on_cross_origin_redirect(
    monkeypatch,
):
    session = FakeSession()
    login_url = f"{SSO_ORIGIN}/login"
    session.add(
        "POST",
        login_url,
        FakeResponse(
            status_code=307,
            headers={"Location": "https://evil.test/credential-capture"},
        ),
    )
    monkeypatch.setattr(rwth, "check_general_connectivity", lambda log: None)
    monkeypatch.setattr(rwth, "check_rwth_status_page", lambda log: None)

    with pytest.raises(SystemExit):
        rwth.post_sso_form(
            session,
            login_url,
            {"j_password": "password"},
            "username/password form",
            logging.getLogger("test.rwth"),
            rwth.sso_url_allowed,
        )

    assert session.calls == [("POST", login_url)]


def test_saml_response_refuses_an_unexpected_form_action():
    session = FakeSession()
    soup = rwth.parse_html(
        """
<form action="https://evil.test/capture">
<input name="RelayState" value="relay">
<input name="SAMLResponse" value="saml">
</form>
"""
    )

    with pytest.raises(SystemExit):
        rwth._submit_saml_response(session, soup, logging.getLogger("test.rwth"))

    assert session.calls == []


def test_login_fetches_provider_otp_only_at_otp_form(
    tmp_path, monkeypatch, no_login_delay
):
    session, posted_login, posted_totp_serial, posted_otp = fresh_login_session()
    ctx = make_context(
        {
            "auth.user": "user",
            "auth.login.totp_serial": "totp",
            "paths.cookie_file": str(tmp_path / "session"),
        }
    )
    ctx.auth.password = "password"

    def otp_resolver():
        assert posted_login
        assert posted_totp_serial == ["totp"]
        assert posted_otp == []
        return "123456"

    ctx.auth.otp_code_resolver = otp_resolver

    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)
    monkeypatch.setattr(rwth, "generate_totp", lambda secret: pytest.fail())
    monkeypatch.setattr("builtins.input", lambda: pytest.fail())
    monkeypatch.setattr(rwth, "save_session", lambda path, cookies, session_key: None)

    rwth.login(ctx)

    assert posted_otp == ["123456"]
    assert ctx.session_key == "abc123"


def test_login_uses_provider_otp_code_before_prompt_or_totp_secret(
    tmp_path,
    monkeypatch,
    no_login_delay,
):
    session, posted_login, posted_totp_serial, posted_otp = fresh_login_session()
    ctx = make_context(
        {
            "auth.user": "user",
            "auth.login.totp_serial": "totp",
            "paths.cookie_file": str(tmp_path / "session"),
        }
    )
    ctx.auth.password = "password"
    ctx.auth.totp_secret = "secret-that-must-not-be-used"
    ctx.auth.otp_code = "123456"

    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)
    monkeypatch.setattr(rwth, "generate_totp", lambda secret: pytest.fail())
    monkeypatch.setattr("builtins.input", lambda: pytest.fail())
    monkeypatch.setattr(rwth, "save_session", lambda path, cookies, session_key: None)

    rwth.login(ctx)

    assert posted_login[0]["j_username"] == "user"
    assert posted_totp_serial == ["totp"]
    assert posted_otp == ["123456"]
    assert ctx.session_key == "abc123"


def test_generated_totp_is_not_echoed(
    tmp_path,
    monkeypatch,
    no_login_delay,
    capsys,
):
    session, _, _, posted_otp = fresh_login_session()
    ctx = make_context(
        {
            "auth.user": "user",
            "auth.login.totp_serial": "totp",
            "paths.cookie_file": str(tmp_path / "session"),
        }
    )
    ctx.auth.password = "password"
    ctx.auth.totp_secret = "totp-secret"

    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)
    monkeypatch.setattr(rwth, "generate_totp", lambda secret: "654321")
    monkeypatch.setattr("builtins.input", lambda: pytest.fail())
    monkeypatch.setattr(rwth, "save_session", lambda path, cookies, session_key: None)

    rwth.login(ctx)

    assert posted_otp == ["654321"]
    output = capsys.readouterr().out
    assert "654321" not in output
    assert "Generated the current TOTP code from the configured seed" in output


@pytest.mark.parametrize(
    ("rejected_step", "expected_message"),
    [
        ("password", "RWTH rejected the username or password"),
        ("serial", "RWTH did not recognize TOTP serial totp"),
        ("totp", "RWTH sign-in failed after TOTP verification"),
    ],
)
def test_login_failure_identifies_the_rejected_sign_in_step(
    tmp_path,
    monkeypatch,
    no_login_delay,
    caplog,
    rejected_step,
    expected_message,
):
    session, _, _, _ = fresh_login_session()
    rejected_url = {
        "password": f"{SSO_ORIGIN}/login",
        "serial": f"{SSO_ORIGIN}/select-token",
        "totp": f"{SSO_ORIGIN}/otp",
    }[rejected_step]
    session.add(
        "POST",
        rejected_url,
        FakeResponse(text="<p>sign-in rejected</p>", url=rejected_url),
    )
    ctx = make_context(
        {
            "auth.user": "user",
            "auth.login.totp_serial": "totp",
            "paths.cookie_file": str(tmp_path / "session"),
        }
    )
    ctx.auth.password = "password"
    ctx.auth.otp_code = "123456"
    caplog.set_level(logging.CRITICAL, logger="syncmymoodle.rwth")

    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)
    monkeypatch.setattr(rwth, "check_rwth_status_page", lambda log: None)

    with pytest.raises(SystemExit) as exc_info:
        rwth.login(ctx)

    assert exc_info.value.code == 1
    assert expected_message in caplog.text
    assert "login-info" not in caplog.text


def test_login_failure_does_not_log_response_html(
    tmp_path,
    monkeypatch,
    caplog,
):
    session = FakeSession()
    session.cookies = []
    session.add(
        "GET",
        f"{MOODLE_URL}auth/shibboleth/index.php",
        FakeResponse(status_code=302, headers={"Location": f"{SSO_ORIGIN}/login"}),
    )
    session.add(
        "GET",
        f"{SSO_ORIGIN}/login",
        FakeResponse(
            text='<input name="diagnostic" value="raw-login-secret">',
        ),
    )
    ctx = make_context(
        {
            "auth.user": "user",
            "paths.cookie_file": str(tmp_path / "session"),
        }
    )
    ctx.auth.password = "password"
    caplog.set_level(logging.INFO, logger="syncmymoodle.rwth")

    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)
    monkeypatch.setattr(rwth, "check_rwth_status_page", lambda log: None)

    with pytest.raises(SystemExit):
        rwth.login(ctx)

    assert "raw-login-secret" not in caplog.text


def test_login_reports_sso_form_timeout_without_traceback(
    tmp_path,
    monkeypatch,
    caplog,
):
    session = FakeSession()
    session.cookies = []
    login_url = f"{SSO_ORIGIN}/login"
    session.add(
        "GET",
        f"{MOODLE_URL}auth/shibboleth/index.php",
        FakeResponse(status_code=302, headers={"Location": login_url}),
    )
    session.add(
        "GET",
        login_url,
        FakeResponse(text='<input name="csrf_token" value="csrf-login">'),
    )

    def timeout(url, kwargs):
        raise requests.Timeout("SSO did not respond")

    session.add("POST", login_url, timeout)
    ctx = make_context(
        {
            "auth.user": "user",
            "paths.cookie_file": str(tmp_path / "session"),
        }
    )
    ctx.auth.password = "password"

    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)
    monkeypatch.setattr(rwth, "check_general_connectivity", lambda log: None)
    monkeypatch.setattr(rwth, "check_rwth_status_page", lambda log: None)

    with pytest.raises(SystemExit) as exc_info:
        rwth.login(ctx)

    assert exc_info.value.code == 1
    assert "SSO did not respond" in caplog.text


def test_login_prompts_for_missing_credentials_only_when_fresh_login_is_needed(
    tmp_path,
    monkeypatch,
    no_login_delay,
    capsys,
):
    session, posted_login, posted_totp_serial, posted_otp = fresh_login_session()
    prompt_answers = iter(["prompt-user", "prompt-totp-serial", "654321"])
    secret_prompts = []
    ctx = make_context(
        {
            "paths.cookie_file": str(tmp_path / "session"),
        }
    )

    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)

    def answer():
        return next(prompt_answers)

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(
        "syncmymoodle.output.getpass.getpass",
        lambda prompt: secret_prompts.append(prompt) or "prompt-password",
    )
    monkeypatch.setattr(rwth, "save_session", lambda path, cookies, session_key: None)

    rwth.login(ctx)

    assert posted_login[0]["j_username"] == "prompt-user"
    assert posted_login[0]["j_password"] == "prompt-password"
    assert posted_totp_serial == ["prompt-totp-serial"]
    assert posted_otp == ["654321"]
    assert ctx.auth.user == "prompt-user"
    assert ctx.auth.password == "prompt-password"
    assert ctx.auth.totp_serial == "prompt-totp-serial"
    assert ctx.config.user is None
    assert ctx.config.totp_serial is None
    captured = capsys.readouterr()
    assert "RWTH SSO username: " in captured.out
    assert secret_prompts == ["RWTH SSO password: "]
    assert captured.err == ""
    assert "RWTH SSO TOTP serial id (for example, TOTP12345678): " in captured.out
    assert "Current 6-digit TOTP code for prompt-totp-serial: " in captured.out
    assert "Selecting TOTP method prompt-totp-serial..." in captured.out


def test_cached_session_status_returns_exact_remaining_time(monkeypatch, tmp_path):
    session = FakeSession()
    session.cookies = requests.cookies.RequestsCookieJar()

    def remaining_response(url, kwargs):
        assert kwargs["params"]["info"] == "core_session_time_remaining"
        assert kwargs["json"] == [
            {
                "index": 0,
                "methodname": "core_session_time_remaining",
                "args": {},
            }
        ]
        return FakeResponse(
            json_payload=[{"error": False, "data": {"timeremaining": 50397}}]
        )

    session.add("POST", rwth.SESSION_REMAINING_URL, remaining_response)
    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(
        rwth,
        "read_private_gzip_json",
        lambda path, description: {
            "format": "syncmymoodle.session.v2",
            "session_key": "sesskey",
            "cookies": [],
        },
    )

    status = rwth.cached_session_status(tmp_path / "session")

    assert status == rwth.SessionStatus(rwth.SessionStatusKind.VALID, 50397)


def test_cached_session_status_reports_expired_session(monkeypatch, tmp_path):
    session = FakeSession()
    session.cookies = requests.cookies.RequestsCookieJar()
    session.add(
        "POST",
        rwth.SESSION_REMAINING_URL,
        FakeResponse(
            json_payload=[{"error": True, "exception": {"errorcode": "invalidsesskey"}}]
        ),
    )
    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(
        rwth,
        "read_private_gzip_json",
        lambda path, description: {
            "format": "syncmymoodle.session.v2",
            "session_key": "sesskey",
            "cookies": [],
        },
    )

    status = rwth.cached_session_status(tmp_path / "session")

    assert status.kind is rwth.SessionStatusKind.EXPIRED


@pytest.mark.parametrize("status_code", [307, 308])
def test_cached_session_status_does_not_resend_session_cross_origin(
    monkeypatch,
    tmp_path,
    status_code,
):
    session = FakeSession()
    session.cookies = requests.cookies.RequestsCookieJar()
    destination = "https://evil.test/collect-session"

    def redirect(url, kwargs):
        del url
        assert kwargs["allow_redirects"] is False
        assert kwargs["params"]["sesskey"] == "browser-sesskey"
        assert kwargs["json"][0]["methodname"] == "core_session_time_remaining"
        return FakeResponse(
            status_code=status_code,
            headers={"Location": destination},
        )

    session.add("POST", rwth.SESSION_REMAINING_URL, redirect)
    session.add(
        "POST",
        destination,
        lambda url, kwargs: pytest.fail(f"session reached {url}: {kwargs}"),
    )
    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(
        rwth,
        "read_private_gzip_json",
        lambda path, description: {
            "format": "syncmymoodle.session.v2",
            "session_key": "browser-sesskey",
            "cookies": [
                {
                    "name": "MoodleSession",
                    "value": "browser-cookie",
                    "domain": "",
                    "path": "/",
                    "secure": True,
                    "expires": None,
                    "rest": {},
                }
            ],
        },
    )

    status = rwth.cached_session_status(tmp_path / "session")

    assert status.kind is rwth.SessionStatusKind.UNKNOWN
    assert "refusing redirect" in (status.detail or "")
    assert session.cookies.get("MoodleSession") == "browser-cookie"
    assert session.calls == [("POST", rwth.SESSION_REMAINING_URL)]


def test_cached_session_status_does_not_probe_legacy_cache(monkeypatch, tmp_path):
    session = FakeSession()
    session.cookies = requests.cookies.RequestsCookieJar()
    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(
        rwth,
        "read_private_gzip_json",
        lambda path, description: {
            "format": "syncmymoodle.cookies.v1",
            "cookies": [],
        },
    )

    status = rwth.cached_session_status(tmp_path / "session")

    assert status.kind is rwth.SessionStatusKind.UNKNOWN
    assert "auth login" in str(status.detail)
    assert session.calls == []
