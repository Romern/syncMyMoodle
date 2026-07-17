import json
import logging
import sys

import pytest
import requests

import syncmymoodle.cli as cli
from syncmymoodle import moodle, sync_handlers
from syncmymoodle.config import Config, ConfigValidationError
from syncmymoodle.moodle import MOODLE_REST_URL
from syncmymoodle.moodle_files import (
    add_moodle_content_file_node,
    add_moodle_file_node,
)
from syncmymoodle.node import Node, RemoteMarkerKind
from syncmymoodle.totp import hotp

from .helpers import FakeResponse, FakeSession, make_context

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


def test_keyring_totp_secret_without_totp_provider_is_rejected():
    with pytest.raises(
        ConfigValidationError, match="auth.login.totp_serial is required"
    ):
        Config.from_dict(
            {
                "auth": {
                    "user": "u",
                    "login": {
                        "provider": "keyring",
                        "keyring_store_totp_secret": True,
                    },
                }
            }
        )


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
    assert "syncmymoodle auth status" in caplog.text
    assert "syncmymoodle auth login" in caplog.text
    assert "delete the cookie file" not in caplog.text


@pytest.mark.parametrize(
    "getter",
    [
        moodle.get_course,
        moodle.get_assignment,
        moodle.get_folders_by_courses,
    ],
    ids=["course", "assignment", "folders"],
)
def test_course_fetchers_skip_course_on_api_error(getter, caplog):
    assert getter(_error_session(), "token", 101) is None
    assert "invalidtoken" in caplog.text


def test_get_course_skips_cleanly_when_request_times_out(caplog):
    session = FakeSession()

    def timeout(url, kwargs):
        raise requests.Timeout("Moodle did not respond")

    session.add("POST", MOODLE_REST_URL, timeout)

    assert moodle.get_course(session, "token", 101) is None
    assert "Moodle did not respond" in caplog.text


def test_course_updates_distinguish_changed_unknown_and_unchanged_modules():
    session = FakeSession()
    module_since = {42: 500, 43: 501, 44: 502, 99: 503}

    def update_response(url, kwargs):
        assert kwargs["data"] == {
            "wstoken": "token",
            "wsfunction": moodle.MOODLE_UPDATE_FUNCTION,
            "courseid": 101,
            "tocheck[0][contextlevel]": "module",
            "tocheck[0][id]": 42,
            "tocheck[0][since]": 500,
            "tocheck[1][contextlevel]": "module",
            "tocheck[1][id]": 43,
            "tocheck[1][since]": 501,
            "tocheck[2][contextlevel]": "module",
            "tocheck[2][id]": 44,
            "tocheck[2][since]": 502,
            "tocheck[3][contextlevel]": "module",
            "tocheck[3][id]": 99,
            "tocheck[3][since]": 503,
        }
        return FakeResponse(
            json_payload={
                "instances": [
                    {
                        "contextlevel": "module",
                        "id": 42,
                        "updates": [{"name": "submissions", "itemids": [7]}],
                    },
                    {
                        "contextlevel": "module",
                        "id": 44,
                        "updates": [],
                    },
                ],
                "warnings": [
                    {
                        "item": "module",
                        "itemid": 99,
                        "warningcode": "missingcallback",
                        "message": "unsupported",
                    }
                ],
            }
        )

    session.add("POST", MOODLE_REST_URL, update_response)

    updates = moodle.check_course_updates(session, "token", 101, module_since)

    assert updates is not None
    assert updates.confirms_unchanged(42, 500) is False
    assert updates.confirms_unchanged(44, 500) is False
    assert updates.confirms_unchanged(99, 500) is False
    assert updates.confirms_unchanged(43, 501) is True
    assert updates.confirms_unchanged(43, 500) is False
    assert updates.confirms_unchanged(100, 501) is False


def test_course_updates_fail_closed_on_non_module_warning():
    session = FakeSession()
    session.add(
        "POST",
        MOODLE_REST_URL,
        FakeResponse(
            json_payload={
                "instances": [],
                "warnings": [{"item": "course", "itemid": 101}],
            }
        ),
    )

    assert moodle.check_course_updates(session, "token", 101, {43: 500}) is None


def test_course_update_api_failure_is_silent_optional_cache_miss(caplog):
    session = FakeSession()
    session.add(
        "POST",
        MOODLE_REST_URL,
        FakeResponse(
            json_payload={
                "exception": "TypeError",
                "message": (
                    "array_keys(): Argument #1 ($array) must be of type array, "
                    "mysqli_native_moodle_recordset given"
                ),
            }
        ),
    )

    assert moodle.check_course_updates(session, "token", 101, {43: 500}) is None
    assert "array_keys" not in caplog.text


def test_course_module_inventory_rejects_malformed_items():
    session = FakeSession()
    session.add(
        "POST",
        MOODLE_REST_URL,
        FakeResponse(json_payload={"quizzes": [None]}),
    )

    assert moodle.get_quizzes_by_course(session, "token", 101) is None


def _module_context():
    ctx = make_context()
    ctx.session = FakeSession()
    course_node = Node("Course", 1, "Course", None)
    section_node = course_node.add_child("Section", 2, "Section")
    assert section_node is not None
    return ctx, sync_handlers.ModuleContext(
        ctx,
        1,
        course_node,
        section_node,
        {},
        {},
    )


def test_module_instance_fetches_a_successful_course_inventory_once():
    ctx, module_context = _module_context()
    calls = []

    def fetch(session, wstoken, course_id):
        calls.append(course_id)
        return [{"coursemodule": 99, "id": 7}]

    for module_id in (41, 42):
        assert (
            sync_handlers.module_instance(
                module_context,
                {"id": module_id},
                ctx.quiz_instance_cache,
                fetch,
            )
            is None
        )

    assert calls == [1]


def test_module_instance_caches_course_inventory_failure():
    ctx, module_context = _module_context()
    calls = []

    def fetch(session, wstoken, course_id):
        calls.append(course_id)
        return None

    for module_id in (41, 42):
        assert (
            sync_handlers.module_instance(
                module_context,
                {"id": module_id},
                ctx.quiz_instance_cache,
                fetch,
            )
            is None
        )

    assert calls == [1]
    assert ctx.incomplete_course_ids == {1}


def test_module_instance_rejects_incomplete_course_inventory():
    ctx, module_context = _module_context()

    assert (
        sync_handlers.module_instance(
            module_context,
            {"id": 41},
            ctx.quiz_instance_cache,
            lambda session, wstoken, course_id: [{}],
        )
        is None
    )
    assert ctx.incomplete_course_ids == {1}


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


def test_submission_response_is_not_logged(caplog):
    payload = {
        "lastattempt": None,
        "feedback": None,
        "diagnostic": "private-assignment-response",
    }
    session = FakeSession()
    session.add(
        "POST",
        MOODLE_REST_URL,
        FakeResponse(text=json.dumps(payload), json_payload=payload),
    )
    caplog.set_level(logging.INFO, logger="syncmymoodle.moodle")

    assert moodle.get_assignment_submission_files(session, "token", 1, 2) == []

    assert "private-assignment-response" not in caplog.text


def test_submission_api_error_is_not_cached_as_an_empty_submission(caplog):
    assert (
        moodle.get_assignment_submission_files(_error_session(), "token", 1, 2) is None
    )
    assert "invalidtoken" in caplog.text


def test_equivalent_moodle_folder_entities_share_one_raw_node():
    parent = Node("Section", 1, "Section", None)

    add_moodle_file_node(
        parent,
        "/R&amp;D/",
        "first.pdf",
        "first",
        "Folder File",
        "https://example.test/first.pdf",
    )
    add_moodle_file_node(
        parent,
        "/R&D/",
        "second.pdf",
        "second",
        "Folder File",
        "https://example.test/second.pdf",
    )

    assert len(parent.children) == 1
    assert parent.children[0].name == "R&amp;D"
    assert [child.name for child in parent.children[0].children] == [
        "first.pdf",
        "second.pdf",
    ]


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


def test_content_file_accepts_only_strong_moodle_content_hashes():
    parent = Node("Section", 1, "Section", None)
    valid = add_moodle_content_file_node(
        parent,
        {
            "filename": "valid.pdf",
            "fileurl": "https://moodle.rwth-aachen.de/pluginfile.php/1/valid.pdf",
            "contenthash": "A" * 40,
        },
    )
    invalid = add_moodle_content_file_node(
        parent,
        {
            "filename": "invalid.pdf",
            "fileurl": "https://moodle.rwth-aachen.de/pluginfile.php/1/invalid.pdf",
            "contenthash": "opaque-revision",
        },
    )

    assert valid is not None
    assert valid.etag == "a" * 40
    assert valid.etag_kind is RemoteMarkerKind.CONTENT_HASH
    assert invalid is not None
    assert invalid.etag is None
    assert invalid.etag_kind is None
