import logging

from syncmymoodle import filters, sync

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
        }
    )
    install_filter_fixtures(monkeypatch, synced_course_ids, FILTER_COURSES)
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
