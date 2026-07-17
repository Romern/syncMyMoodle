import json
from collections.abc import Callable
from typing import Any

import pytest

from syncmymoodle import moodle

from .helpers import FakeResponse, FakeSession


def webservice_session(
    function: str,
    request_data: dict[str, Any],
    payload: Any,
) -> FakeSession:
    session = FakeSession()

    def respond(url: str, kwargs: dict[str, Any]) -> FakeResponse:
        del url
        assert kwargs["params"] == {
            "moodlewsrestformat": "json",
            "wsfunction": function,
        }
        assert kwargs["data"] == {
            "wstoken": "webservice-token",
            "wsfunction": function,
            **request_data,
        }
        return FakeResponse(json_payload=payload)

    session.add("POST", moodle.MOODLE_REST_URL, respond)
    return session


def test_get_all_courses_uses_mobile_batch_contract():
    courses = [{"id": 101, "fullname": "Course"}]
    request_data = {
        "requests[0][function]": "core_enrol_get_users_courses",
        "requests[0][arguments]": json.dumps(
            {"userid": "10001", "returnusercount": "0"}
        ),
        "requests[0][settingfilter]": 1,
        "requests[0][settingfileurl]": 1,
    }
    session = webservice_session(
        "tool_mobile_call_external_functions",
        request_data,
        {"responses": [{"error": False, "data": json.dumps(courses)}]},
    )

    assert moodle.get_all_courses(session, "webservice-token", 10001) == courses


def test_get_direct_course_roles_uses_mobile_batch_contract():
    request_data = {
        "requests[0][function]": "core_user_get_course_user_profiles",
        "requests[0][arguments]": json.dumps(
            {"userlist": [{"userid": "10001", "courseid": "101"}]}
        ),
        "requests[0][settingfilter]": 0,
        "requests[0][settingfileurl]": 0,
        "requests[1][function]": "core_user_get_course_user_profiles",
        "requests[1][arguments]": json.dumps(
            {"userlist": [{"userid": "10001", "courseid": "102"}]}
        ),
        "requests[1][settingfilter]": 0,
        "requests[1][settingfileurl]": 0,
    }
    session = webservice_session(
        "tool_mobile_call_external_functions",
        request_data,
        {
            "responses": [
                {
                    "error": False,
                    "data": json.dumps(
                        [{"id": 10001, "roles": [{"shortname": "Student"}]}]
                    ),
                },
                {
                    "error": False,
                    "data": json.dumps([{"id": 10001, "roles": []}]),
                },
            ]
        },
    )

    assert moodle.get_direct_course_roles_by_course(
        session,
        "webservice-token",
        10001,
        [101, 102],
    ) == {"101": {"Student"}, "102": set()}


def test_get_course_uses_content_contract():
    contents = [{"id": 201, "name": "General", "modules": []}]
    session = webservice_session(
        "core_course_get_contents",
        {
            "courseid": 101,
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
        },
        contents,
    )

    assert moodle.get_course(session, "webservice-token", "101") == contents


def test_get_assignment_uses_course_inventory_contract():
    assignment_course = {"id": 101, "assignments": [{"id": 301}]}
    session = webservice_session(
        "mod_assign_get_assignments",
        {
            "courseids[0]": 101,
            "includenotenrolledcourses": 1,
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
        },
        {"courses": [assignment_course]},
    )

    assert moodle.get_assignment(session, "webservice-token", "101") == (
        assignment_course
    )


def test_get_assignment_submission_files_uses_status_contract():
    graded_file = {"filename": "graded.pdf"}
    session = webservice_session(
        "mod_assign_get_submission_status",
        {
            "assignid": 301,
            "userid": 10001,
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
        },
        {
            "feedback": {
                "plugins": [
                    {"fileareas": [{"area": "feedback_files", "files": [graded_file]}]}
                ]
            }
        },
    )

    assert moodle.get_assignment_submission_files(
        session,
        "webservice-token",
        10001,
        301,
    ) == [graded_file]


def test_get_folders_uses_course_inventory_contract():
    folders = [{"coursemodule": 401, "name": "Files"}]
    session = webservice_session(
        "mod_folder_get_folders_by_courses",
        {
            "courseids[0]": "101",
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
        },
        {"folders": folders},
    )

    assert moodle.get_folders_by_courses(session, "webservice-token", 101) == folders


@pytest.mark.parametrize(
    ("getter", "function", "response_key"),
    [
        (moodle.get_ltis_by_course, "mod_lti_get_ltis_by_courses", "ltis"),
        (
            moodle.get_h5pactivities_by_course,
            "mod_h5pactivity_get_h5pactivities_by_courses",
            "h5pactivities",
        ),
        (
            moodle.get_quizzes_by_course,
            "mod_quiz_get_quizzes_by_courses",
            "quizzes",
        ),
    ],
)
def test_get_module_inventories_use_course_contract(
    getter: Callable[[FakeSession, str, int], list[dict[str, Any]] | None],
    function: str,
    response_key: str,
):
    inventory = [{"id": 501, "coursemodule": 601}]
    session = webservice_session(
        function,
        {"courseids[0]": 101},
        {response_key: inventory},
    )

    assert getter(session, "webservice-token", 101) == inventory


def test_get_lti_launch_data_uses_tool_contract():
    launch_data = {
        "endpoint": "https://video.example.test/lti",
        "parameters": [],
    }
    session = webservice_session(
        "mod_lti_get_tool_launch_data",
        {"toolid": 501},
        launch_data,
    )

    assert moodle.get_lti_launch_data(session, "webservice-token", 501) == launch_data


def test_get_quiz_attempts_requests_finished_non_preview_attempts():
    attempts = [{"id": 701, "state": "finished"}]
    session = webservice_session(
        "mod_quiz_get_user_attempts",
        {"quizid": 601, "status": "finished", "includepreviews": 0},
        {"attempts": attempts},
    )

    assert moodle.get_quiz_attempts(session, "webservice-token", 601) == attempts


def test_get_quiz_attempts_rejects_partially_malformed_inventory():
    session = webservice_session(
        "mod_quiz_get_user_attempts",
        {"quizid": 601, "status": "finished", "includepreviews": 0},
        {"attempts": [{"id": 701}, "private-malformed-attempt"]},
    )

    assert moodle.get_quiz_attempts(session, "webservice-token", 601) is None


def test_get_quiz_attempt_review_requests_every_page():
    review = {"grade": "8.00", "questions": [{"slot": 1}]}
    session = webservice_session(
        "mod_quiz_get_attempt_review",
        {"attemptid": 701, "page": -1},
        review,
    )

    assert moodle.get_quiz_attempt_review(session, "webservice-token", 701) == review
