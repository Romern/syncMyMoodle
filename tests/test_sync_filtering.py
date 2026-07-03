from .helpers import FakeSession, make_syncer, node_rows

FILTER_COURSES = [
    {"id": 201, "shortname": "Current Semester", "idnumber": "26ss-current"},
    {"id": 202, "shortname": "Selected Old Semester", "idnumber": "25ws-selected"},
    {"id": 203, "shortname": "Skipped Current Semester", "idnumber": "26ss-skipped"},
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
    syncer = make_syncer(
        {
            "selected_courses": [
                "https://moodle.rwth-aachen.de/course/view.php?id=202"
            ],
            "only_sync_semester": ["26ss"],
        }
    )
    install_filter_fixtures(monkeypatch, synced_course_ids, FILTER_COURSES)
    syncer.ctx.session = FakeSession()

    syncer.sync()

    assert synced_course_ids == [202]
    assert node_rows(syncer.ctx.root_node) == [
        "Semester | 25ws |  |  | ",
        "Course | 25ws/Selected Old Semester |  |  | ",
    ]


def test_skip_courses_and_semester_filter_limit_synced_courses(monkeypatch):
    synced_course_ids = []
    syncer = make_syncer(
        {
            "skip_courses": ["https://moodle.rwth-aachen.de/course/view.php?id=203"],
            "only_sync_semester": ["26ss"],
        }
    )
    install_filter_fixtures(monkeypatch, synced_course_ids, FILTER_COURSES)
    syncer.ctx.session = FakeSession()

    syncer.sync()

    assert synced_course_ids == [201]
    assert node_rows(syncer.ctx.root_node) == [
        "Semester | 26ss |  |  | ",
        "Course | 26ss/Current Semester |  |  | ",
    ]


# Course ids that are substrings of one another, to pin down exact-id matching.
SUBSTRING_COURSES = [
    {"id": 1, "shortname": "Course One", "idnumber": "26ss-1"},
    {"id": 2, "shortname": "Course Two", "idnumber": "26ss-2"},
    {"id": 12, "shortname": "Course Twelve", "idnumber": "26ss-12"},
    {"id": 123, "shortname": "Course OneTwoThree", "idnumber": "26ss-123"},
]


def _run_filter(config, monkeypatch):
    synced = []
    syncer = make_syncer(config)
    install_filter_fixtures(monkeypatch, synced, SUBSTRING_COURSES)
    syncer.ctx.session = FakeSession()
    syncer.sync()
    return synced


def test_selected_courses_match_by_exact_id_not_substring(monkeypatch):
    # Selecting course 12 must not also pull in courses 1 and 2.
    synced = _run_filter(
        {"selected_courses": ["https://moodle.rwth-aachen.de/course/view.php?id=12"]},
        monkeypatch,
    )
    assert synced == [12]


def test_skip_courses_match_by_exact_id_not_substring(monkeypatch):
    # Skipping course 12 must not silently drop courses 1 and 2.
    synced = _run_filter(
        {"skip_courses": ["https://moodle.rwth-aachen.de/course/view.php?id=12"]},
        monkeypatch,
    )
    assert synced == [1, 2, 123]


def test_bare_numeric_id_entry_is_accepted(monkeypatch):
    synced = _run_filter({"selected_courses": ["12"]}, monkeypatch)
    assert synced == [12]


def test_selected_courses_override_skip_courses(monkeypatch):
    # A course present in both lists is synced: selected_courses wins.
    url = "https://moodle.rwth-aachen.de/course/view.php?id=12"
    synced = _run_filter(
        {"selected_courses": [url], "skip_courses": [url]}, monkeypatch
    )
    assert synced == [12]
