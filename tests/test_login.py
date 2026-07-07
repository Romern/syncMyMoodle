import pytest

import syncmymoodle.rwth as rwth
from syncmymoodle.constants import MOODLE_URL

from .helpers import FakeResponse, FakeSession, make_context


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
    saved_cookie_paths = []
    ctx = make_context(
        {
            "downloads.dry_run": dry_run,
            "paths.cookie_file": str(tmp_path / "session"),
        }
    )

    monkeypatch.setattr(rwth.requests, "Session", lambda: session)
    monkeypatch.setattr(rwth, "check_moodle_availability", lambda session, log: None)
    monkeypatch.setattr(
        rwth,
        "save_session_cookies",
        lambda path, cookies: saved_cookie_paths.append(path),
    )

    rwth.login(ctx)

    assert ctx.session_key == "abc123"
    assert len(saved_cookie_paths) == expected_saves
