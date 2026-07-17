import json
import logging

import pytest

from syncmymoodle import filters, moodle, sync

from .helpers import FakeSession, make_context, node_rows

FILTER_COURSES = [
    {"id": 201, "shortname": "Current Semester", "idnumber": "26ss-current"},
    {"id": 202, "shortname": "Selected Old Semester", "idnumber": "25ws-selected"},
    {"id": 203, "shortname": "Skipped Current Semester", "idnumber": "26ss-skipped"},
]


def filtered_rows(syncer):
    return [
        (item.config_key, item.category, item.item, item.reason)
        for item in sorted(syncer.filtered_items)
    ]


def install_filter_fixtures(monkeypatch, synced_course_ids, courses):
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_all_courses",
        lambda session, wstoken, user_id: courses,
    )
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_course",
        lambda session, wstoken, course_id: synced_course_ids.append(course_id) or [],
    )


def test_selected_courses_override_semester_filter(monkeypatch):
    synced_course_ids = []
    syncer = make_context(
        {
            "courses.selected": [
                "https://moodle.rwth-aachen.de/course/view.php?id=202"
            ],
            "courses.semesters": ["26ss"],
            "courses.exclude_roles": ["tutor"],
        }
    )
    install_filter_fixtures(monkeypatch, synced_course_ids, FILTER_COURSES)
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_direct_course_roles_by_course",
        lambda *args, **kwargs: pytest.fail(
            "an explicit course selection must not perform role filtering"
        ),
    )
    syncer.session = FakeSession()

    sync.sync(syncer)

    assert synced_course_ids == [202]
    assert node_rows(syncer.root_node) == [
        "Semester | 25ws |  |  |",
        "Course | 25ws/Selected Old Semester |  |  |",
    ]
    assert filtered_rows(syncer) == [
        (
            "courses.selected",
            "course",
            "Current Semester (201)",
            "not in the configured selection",
        ),
        (
            "courses.selected",
            "course",
            "Skipped Current Semester (203)",
            "not in the configured selection",
        ),
    ]


def test_sync_does_not_log_raw_moodle_payloads(monkeypatch, caplog):
    courses = [
        {
            "id": 201,
            "shortname": "Course",
            "idnumber": "26ss-current",
            "diagnostic": "private-course-payload",
        }
    ]
    syncer = make_context()
    install_filter_fixtures(monkeypatch, [], courses)
    syncer.session = FakeSession()
    caplog.set_level(logging.INFO, logger="syncmymoodle.sync")

    sync.sync(syncer)

    assert "private-course-payload" not in caplog.text


def test_malformed_course_summary_does_not_block_later_course(monkeypatch, caplog):
    synced_course_ids = []
    valid = {
        "id": 201,
        "shortname": "Valid Course",
        "idnumber": "26ss-current",
    }
    syncer = make_context()
    install_filter_fixtures(
        monkeypatch,
        synced_course_ids,
        [
            "private-malformed-summary",
            {"id": True},
            {"id": 199, "shortname": ["private-malformed-name"]},
            valid,
        ],
    )
    syncer.session = FakeSession()
    caplog.set_level(logging.ERROR, logger="syncmymoodle.sync")

    sync.sync(syncer)

    assert synced_course_ids == [201]
    assert syncer.stats.failed == 3
    assert node_rows(syncer.root_node) == [
        "Semester | 26ss |  |  |",
        "Course | 26ss/Valid Course |  |  |",
    ]
    assert "private-malformed-summary" not in caplog.text


@pytest.mark.parametrize("module_kind", ["assign", "folder"])
def test_malformed_auxiliary_inventory_marks_course_incomplete(
    module_kind, monkeypatch, caplog
):
    course_id = 201
    module_id = 401
    syncer = make_context(
        {
            "modules.assignment": module_kind == "assign",
            "modules.folder": module_kind == "folder",
        }
    )
    syncer.session = FakeSession()
    install_filter_fixtures(
        monkeypatch,
        [],
        [{"id": course_id, "shortname": "Course", "idnumber": "26ss"}],
    )
    monkeypatch.setattr(
        moodle,
        "get_course",
        lambda *args: [
            {
                "id": 301,
                "name": "General",
                "modules": [
                    {
                        "id": module_id,
                        "instance": 501,
                        "modname": module_kind,
                        "name": "Module",
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(
        moodle,
        "get_assignment",
        lambda *args: {
            "assignments": [
                {"id": 501, "cmid": module_id},
                "private-malformed-assignment",
            ]
        },
    )
    monkeypatch.setattr(
        moodle,
        "get_folders_by_courses",
        lambda *args: [
            {"coursemodule": module_id},
            {"coursemodule": module_id},
        ],
    )
    caplog.set_level(logging.ERROR, logger="syncmymoodle.sync")

    sync.sync(syncer)

    assert syncer.incomplete_course_ids == {course_id}
    assert syncer.stats.failed == 1
    assert "private-malformed-assignment" not in caplog.text


def test_verbose_sync_logging_identifies_slow_modules(monkeypatch, caplog):
    course = {
        "id": 201,
        "shortname": "Course",
        "idnumber": "26ss-current",
    }
    sections = [
        {
            "id": 301,
            "name": "Week one",
            "modules": [{"id": 401, "name": "Lecture recordings", "modname": "lti"}],
        }
    ]
    syncer = make_context()
    syncer.session = FakeSession()
    monkeypatch.setattr(moodle, "get_all_courses", lambda *args: [course])
    monkeypatch.setattr(moodle, "get_course", lambda *args: sections)
    monkeypatch.setattr(sync.sync_handlers, "handle_module", lambda *args: None)
    timestamps = iter([10.0, 11.25])
    monkeypatch.setattr(sync.time, "monotonic", lambda: next(timestamps))
    caplog.set_level(logging.INFO, logger="syncmymoodle.sync")

    sync.sync(syncer)

    assert caplog.messages == [
        "Processed Moodle module 401 (lti) 'Lecture recordings' in 1.2s"
    ]


def test_skip_courses_and_semester_filter_limit_synced_courses(monkeypatch):
    synced_course_ids = []
    syncer = make_context(
        {
            "courses.skip": ["https://moodle.rwth-aachen.de/course/view.php?id=203"],
            "courses.semesters": ["26ss"],
        }
    )
    install_filter_fixtures(monkeypatch, synced_course_ids, FILTER_COURSES)
    syncer.session = FakeSession()

    sync.sync(syncer)

    assert synced_course_ids == [201]
    assert node_rows(syncer.root_node) == [
        "Semester | 26ss |  |  |",
        "Course | 26ss/Current Semester |  |  |",
    ]
    assert filtered_rows(syncer) == [
        (
            "courses.semesters",
            "course",
            "Selected Old Semester (202)",
            "semester '25ws' is not selected",
        ),
        (
            "courses.skip",
            "course",
            "Skipped Current Semester (203)",
            "matches 'https://moodle.rwth-aachen.de/course/view.php?id=203'",
        ),
    ]


def test_excluded_course_roles_skip_matches_and_keep_unknowns(monkeypatch, capsys):
    synced_course_ids = []
    role_lookup_batches = []
    syncer = make_context({"courses.exclude_roles": ["Tutor"]})
    install_filter_fixtures(monkeypatch, synced_course_ids, FILTER_COURSES)
    roles_by_course = {
        "201": {" Student "},
        "202": {"student", "TUTOR"},
        "203": None,
    }

    def get_direct_course_roles_by_course(session, wstoken, user_id, course_ids, log):
        role_lookup_batches.append(course_ids)
        return roles_by_course

    monkeypatch.setattr(
        "syncmymoodle.moodle.get_direct_course_roles_by_course",
        get_direct_course_roles_by_course,
    )
    syncer.session = FakeSession()

    sync.sync(syncer)

    assert role_lookup_batches == [[201, 202, 203]]
    assert synced_course_ids == [201, 203]
    output = capsys.readouterr().out
    assert "Scanning 2 courses..." in output
    assert "[1/2] Scanning Current Semester..." in output
    assert "[2/2] Scanning Skipped Current Semester..." in output
    assert filtered_rows(syncer) == [
        (
            "courses.exclude_roles",
            "course",
            "Selected Old Semester (202)",
            "your directly assigned Moodle course role is 'tutor'",
        )
    ]


@pytest.mark.parametrize(
    "profile_payload",
    [
        [{"id": 17}],
        [{"id": 17, "roles": [{"shortname": "student"}, {"name": "Broken"}]}],
        {"id": 17, "roles": []},
    ],
)
def test_get_direct_course_roles_keeps_malformed_profiles_unknown(
    monkeypatch, caplog, profile_payload
):
    monkeypatch.setattr(
        moodle,
        "call_webservice",
        lambda *args, **kwargs: {
            "responses": [{"error": False, "data": json.dumps(profile_payload)}]
        },
    )

    assert moodle.get_direct_course_roles_by_course(
        FakeSession(), "token", 17, [201]
    ) == {"201": None}
    assert "profile roles were missing or malformed" in caplog.text


def test_get_direct_course_roles_stops_after_batch_error(monkeypatch, caplog):
    calls = []

    def call_webservice(*args, **kwargs):
        calls.append((args, kwargs))
        return {"responses": [{"error": True, "exception": "not allowed"}]}

    monkeypatch.setattr(moodle, "call_webservice", call_webservice)

    assert moodle.get_direct_course_roles_by_course(
        FakeSession(), "token", 17, [201, 202, 203]
    ) == {"201": None, "202": None, "203": None}
    assert len(calls) == 1
    assert caplog.text.count("not allowed") == 1


def test_section_and_module_filters_record_the_matching_patterns():
    syncer = make_context(
        {
            "filters.exclude_sections": {"12": ["Hidden*"]},
            "filters.exclude_modules": {"12": ["Skip Module"]},
        }
    )

    assert filters.should_skip_section(syncer, {"id": 3, "name": "Hidden Week"}, 12)
    assert filters.should_skip_module(
        syncer,
        {"id": 4, "name": "Skip Module", "modname": "resource"},
        12,
    )

    assert filtered_rows(syncer) == [
        (
            "filters.exclude_modules",
            "module",
            "Skip Module (4) in course 12",
            "matches 'Skip Module'",
        ),
        (
            "filters.exclude_sections",
            "section",
            "Hidden Week (3) in course 12",
            "matches 'Hidden*'",
        ),
    ]


def test_filtered_urls_are_deduplicated_and_secrets_are_redacted():
    syncer = make_context({"filters.exclude_links": ["*private.pdf*"]})
    url = "https://files.example.test/private.pdf?token=super-secret"

    assert filters.should_skip_url(syncer, url, "resource link")
    assert filters.should_skip_url(syncer, url, "resource link")

    assert len(syncer.filtered_items) == 1
    (item,) = syncer.filtered_items
    assert item.config_key == "filters.exclude_links"
    assert "token=[REDACTED]" in item.item
    assert "super-secret" not in item.item


def test_course_specific_link_filters_apply_only_to_their_course():
    syncer = make_context(
        {
            "filters.exclude_links": {"12": ["*private.pdf"]},
            "filters.allowed_domains": {"12": ["files.example.test"]},
        }
    )

    assert filters.should_skip_url(
        syncer,
        "https://files.example.test/private.pdf",
        course_id=12,
    )
    assert filters.should_skip_url(
        syncer,
        "https://other.example.test/public.pdf",
        course_id=12,
    )
    assert not filters.should_skip_url(
        syncer,
        "https://other.example.test/private.pdf",
        course_id=13,
    )


# Course ids that are substrings of one another, to pin down exact-id matching.
SUBSTRING_COURSES = [
    {"id": 1, "shortname": "Course One", "idnumber": "26ss-1"},
    {"id": 2, "shortname": "Course Two", "idnumber": "26ss-2"},
    {"id": 12, "shortname": "Course Twelve", "idnumber": "26ss-12"},
    {"id": 123, "shortname": "Course OneTwoThree", "idnumber": "26ss-123"},
]


def _run_filter(config, monkeypatch):
    synced = []
    syncer = make_context(config)
    install_filter_fixtures(monkeypatch, synced, SUBSTRING_COURSES)
    syncer.session = FakeSession()
    sync.sync(syncer)
    return synced


def test_selected_courses_match_by_exact_id_not_substring(monkeypatch):
    # Selecting course 12 must not also pull in courses 1 and 2.
    synced = _run_filter(
        {"courses.selected": ["https://moodle.rwth-aachen.de/course/view.php?id=12"]},
        monkeypatch,
    )
    assert synced == [12]


def test_skip_courses_match_by_exact_id_not_substring(monkeypatch):
    # Skipping course 12 must not silently drop courses 1 and 2.
    synced = _run_filter(
        {"courses.skip": ["https://moodle.rwth-aachen.de/course/view.php?id=12"]},
        monkeypatch,
    )
    assert synced == [1, 2, 123]


def test_bare_numeric_id_entry_is_accepted(monkeypatch):
    synced = _run_filter({"courses.selected": ["12"]}, monkeypatch)
    assert synced == [12]


def test_selected_courses_override_skip_courses(monkeypatch):
    # A course present in both lists is synced: selected_courses wins.
    url = "https://moodle.rwth-aachen.de/course/view.php?id=12"
    synced = _run_filter(
        {"courses.selected": [url], "courses.skip": [url]}, monkeypatch
    )
    assert synced == [12]
