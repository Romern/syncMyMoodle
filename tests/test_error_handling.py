import sys
from types import SimpleNamespace

import pytest

import syncmymoodle.cli as cli
from syncmymoodle import moodle
from syncmymoodle.moodle import MOODLE_REST_URL
from syncmymoodle.moodle_files import add_moodle_content_file_node
from syncmymoodle.node import Node
from syncmymoodle.totp import hotp

from .helpers import FakeResponse, FakeSession

INVALID_TOKEN_PAYLOAD = {
    "exception": "moodle_exception",
    "errorcode": "invalidtoken",
    "message": "Invalid token - token expired",
}


# --------------------------------------------------------------------------
# cli: explicit --config pointing at a missing file must fail clearly
# --------------------------------------------------------------------------


def test_missing_config_file_fails_clearly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["syncmymoodle", "--config", str(tmp_path / "nope.json")]
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    # argparse error exits with 2 and prints to stderr; previously this was an
    # UnboundLocalError traceback.
    assert excinfo.value.code == 2
    assert "config file not found" in capsys.readouterr().err


# --------------------------------------------------------------------------
# cli: keyring TOTP guard must check the real config key
# --------------------------------------------------------------------------


def test_keyring_totp_secret_without_totp_provider_is_rejected(
    tmp_path, monkeypatch, capsys
):
    keyring_calls = []
    fake_keyring = SimpleNamespace(
        get_password=lambda service, name: keyring_calls.append(name),
        set_password=lambda service, name, value: keyring_calls.append(name),
    )
    monkeypatch.setattr(cli, "keyring", fake_keyring)
    # Keep the run from picking up real config files.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["syncmymoodle", "--secretservice", "--secretservicetotpsecret", "--user", "u"],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    # The guard used to check a key that is never set ("secretservicetotpsecret"),
    # letting the run continue into keyring lookups keyed on None.
    assert excinfo.value.code == 1
    assert "TOTP provider" in capsys.readouterr().out
    assert keyring_calls == []


# --------------------------------------------------------------------------
# moodle: API error payloads must not raise KeyError tracebacks
# --------------------------------------------------------------------------


def _error_session():
    session = FakeSession()
    session.add(
        "POST", MOODLE_REST_URL, FakeResponse(json_payload=INVALID_TOKEN_PAYLOAD)
    )
    return session


def test_get_all_courses_exits_clearly_on_api_error(caplog):
    with pytest.raises(SystemExit) as excinfo:
        moodle.get_all_courses(_error_session(), "token", 1)

    assert excinfo.value.code == 1
    assert "invalidtoken" in caplog.text


def test_get_course_skips_course_on_api_error(caplog):
    assert moodle.get_course(_error_session(), "token", 101) == []
    assert "invalidtoken" in caplog.text


def test_get_assignment_skips_course_on_api_error(caplog):
    assert moodle.get_assignment(_error_session(), "token", 101) is None
    assert "invalidtoken" in caplog.text


def test_get_folders_skips_course_on_api_error(caplog):
    assert moodle.get_folders_by_courses(_error_session(), "token", 101) == []
    assert "invalidtoken" in caplog.text


def test_submission_files_tolerate_null_fields():
    # Moodle may serialize optional structures as JSON null; .get(key, {})
    # does not guard against that.
    session = FakeSession()
    session.add(
        "POST",
        MOODLE_REST_URL,
        FakeResponse(
            json_payload={
                "lastattempt": {"submission": None, "teamsubmission": None},
                "feedback": {
                    "plugins": [
                        {
                            "fileareas": [
                                {"files": [{"filename": "f.pdf"}]},  # no "area"
                                {
                                    "area": "feedback_files",
                                    "files": [{"filename": "graded.pdf"}],
                                },
                            ]
                        }
                    ]
                },
            }
        ),
    )

    files = moodle.get_assignment_submission_files(session, "token", 1, 2)
    assert files == [{"filename": "graded.pdf"}]


# --------------------------------------------------------------------------
# totp: secrets pasted with grouping separators must work
# --------------------------------------------------------------------------


def test_hotp_accepts_formatted_secrets():
    clean = hotp("GEZDGNBVGY3TQOJQ", 42)
    assert hotp("gezd gnbv gy3t qojq", 42) == clean
    assert hotp("GEZD-GNBV-GY3T-QOJQ", 42) == clean


# --------------------------------------------------------------------------
# moodle_files: unnamed content files must not produce a None node name
# --------------------------------------------------------------------------


def test_content_file_without_filename_gets_placeholder_name():
    parent = Node("Section", 1, "Section", None)
    node = add_moodle_content_file_node(
        parent,
        {"fileurl": "https://moodle.rwth-aachen.de/pluginfile.php/1/dir/"},
    )
    assert node is not None
    assert node.name == "file"
