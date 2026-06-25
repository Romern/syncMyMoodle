from .helpers import FakeSession, make_syncer, node_rows

FILTER_COURSES = [
    {"id": 201, "shortname": "Current Semester", "idnumber": "26ss-current"},
    {"id": 202, "shortname": "Selected Old Semester", "idnumber": "25ws-selected"},
    {"id": 203, "shortname": "Skipped Current Semester", "idnumber": "26ss-skipped"},
]


def test_selected_courses_override_semester_filter():
    synced_course_ids = []
    syncer = make_syncer(
        {
            "selected_courses": [
                "https://moodle.rwth-aachen.de/course/view.php?id=202"
            ],
            "only_sync_semester": ["26ss"],
        }
    )
    syncer.get_all_courses = lambda: FILTER_COURSES  # type: ignore[method-assign]
    syncer.get_course = lambda course_id: synced_course_ids.append(course_id) or []  # type: ignore[method-assign]
    syncer.session = FakeSession()

    syncer.sync()

    assert synced_course_ids == [202]
    assert node_rows(syncer.root_node) == [
        "Semester | 25ws |  |  | ",
        "Course | 25ws/Selected Old Semester |  |  | ",
    ]


def test_skip_courses_and_semester_filter_limit_synced_courses():
    synced_course_ids = []
    syncer = make_syncer(
        {
            "skip_courses": ["https://moodle.rwth-aachen.de/course/view.php?id=203"],
            "only_sync_semester": ["26ss"],
        }
    )
    syncer.get_all_courses = lambda: FILTER_COURSES  # type: ignore[method-assign]
    syncer.get_course = lambda course_id: synced_course_ids.append(course_id) or []  # type: ignore[method-assign]
    syncer.session = FakeSession()

    syncer.sync()

    assert synced_course_ids == [201]
    assert node_rows(syncer.root_node) == [
        "Semester | 26ss |  |  | ",
        "Course | 26ss/Current Semester |  |  | ",
    ]
