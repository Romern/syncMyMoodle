import logging

import pytest
import requests

import syncmymoodle.rwth as rwth
from syncmymoodle.constants import MOODLE_URL
from syncmymoodle.context import MoodleAccount
from syncmymoodle.moodle_tokens import MoodleTokens

from .helpers import FakeResponse, FakeSession, make_context


def fresh_login_session():
    session = FakeSession()
    session.cookies = []
    login_url = "https://sso.example/login"
    select_url = "https://sso.example/select-token"
    otp_url = "https://sso.example/otp"
    posted_login = []
    posted_totp_serial = []
    posted_otp = []

    session.add(
        "GET",
        f"{MOODLE_URL}auth/shibboleth/index.php",
        FakeResponse(
            text='<input name="csrf_token" value="csrf-login">',
            url=login_url,
        ),
    )

    def login_response(url, kwargs):
        del url
        posted_login.append(kwargs["data"])
        return FakeResponse(
            text="""
<input id="fudis_selected_token_ids_input">
<input name="csrf_token" value="csrf-select">
""",
            url=select_url,
        )

    session.add("POST", login_url, login_response)

    def select_response(url, kwargs):
        del url
        posted_totp_serial.append(kwargs["data"]["fudis_selected_token_ids_input"])
        return FakeResponse(
            text="""
<input id="fudis_otp_input">
<input name="csrf_token" value="csrf-otp">
""",
            url=otp_url,
        )

    session.add("POST", select_url, select_response)

    def otp_response(url, kwargs):
        del url
        posted_otp.append(kwargs["data"]["fudis_otp_input"])
        return FakeResponse(
            text="""
<input name="RelayState" value="relay">
<input name="SAMLResponse" value="saml">
""",
            url=otp_url,
        )

    session.add("POST", otp_url, otp_response)
    session.add(
        "POST",
        f"{MOODLE_URL}Shibboleth.sso/SAML2/POST",
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
        FakeResponse(
            text='<script>{"sesskey":"abc123"}</script>',
            url=f"{MOODLE_URL}my/",
        ),
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
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail())
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
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail())
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
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail())
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
        "password": "https://sso.example/login",
        "serial": "https://sso.example/select-token",
        "totp": "https://sso.example/otp",
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
        FakeResponse(
            text='<input name="diagnostic" value="raw-login-secret">',
            url="https://sso.example/login",
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
    login_url = "https://sso.example/login"
    session.add(
        "GET",
        f"{MOODLE_URL}auth/shibboleth/index.php",
        FakeResponse(
            text='<input name="csrf_token" value="csrf-login">',
            url=login_url,
        ),
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
    prompts = []
    ctx = make_context(
        {
            "paths.cookie_file": str(tmp_path / "session"),
        }
    )

    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)

    def answer(prompt):
        prompts.append(prompt)
        return next(prompt_answers)

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(rwth.getpass, "getpass", lambda prompt: "prompt-password")
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
    assert "RWTH SSO TOTP serial id (for example, TOTP12345678): " in prompts
    assert "Current 6-digit TOTP code for prompt-totp-serial: " in prompts
    assert "Selecting TOTP method prompt-totp-serial..." in capsys.readouterr().out


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
