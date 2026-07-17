import io
import logging
import urllib.parse
import zipfile
from typing import Any

import requests

from syncmymoodle import (
    course_cache,
    links,
    moodle,
    opencast,
    sciebo,
    sync,
    sync_handlers,
)
from syncmymoodle.constants import HTTP_TIMEOUT_SECONDS
from syncmymoodle.context import MoodleAccount
from syncmymoodle.moodle_tokens import MoodleTokens
from syncmymoodle.node import Node

from .helpers import (
    FakeResponse,
    FakeSession,
    assert_snapshot,
    install_moodle_fixtures,
    load_fixture,
    load_json_fixture,
    make_context,
    node_at_path,
    node_rows,
)

H5P_PACKAGE_URL = "https://moodle.rwth-aachen.de/pluginfile.php/activity.h5p"
PAGE_URL = "https://moodle.rwth-aachen.de/mod/page/view.php?id=123"
PAGE_CONTENT_URL = (
    "https://moodle.rwth-aachen.de/pluginfile.php/1/mod_page/content/315/index.html"
)


def opencast_episode_entry(
    episode_id: str,
    url: str,
    *,
    series_id: str | None = None,
    title: str | None = None,
    checksum: str | None = None,
) -> dict[str, Any]:
    track: dict[str, Any] = {
        "type": "presentation/delivery",
        "mimetype": "video/mp4",
        "url": url,
        "video": {"resolution": "1920x1080"},
    }
    if checksum is not None:
        track["checksum"] = {"type": "md5", "$": checksum}
    mediapackage: dict[str, Any] = {
        "id": episode_id,
        "media": {"track": track},
    }
    if series_id is not None:
        mediapackage["series"] = series_id
    if title is not None:
        mediapackage["title"] = title
    return {"mediapackage": mediapackage}


def run_page_handler(response):
    ctx = make_context({"links.opencast": False})
    session = FakeSession()
    session.add("GET", PAGE_URL, response)
    ctx.session = session
    course_node = Node("Course", 1, "Course", None)
    section_node = course_node.add_child("Section", 2, "Section")
    assert section_node is not None
    module_context = sync_handlers.ModuleContext(
        ctx, 1, course_node, section_node, {}, {}
    )

    sync_handlers.handle_embedded_link_module(
        module_context,
        {
            "id": 123,
            "modname": "page",
            "name": "Unavailable page",
            "url": PAGE_URL,
        },
    )

    return ctx


def test_page_request_failure_is_counted(caplog):
    def fail_request(url, kwargs):
        raise requests.ConnectionError("offline")

    ctx = run_page_handler(fail_request)

    assert ctx.stats.failed == 1
    assert "Failed to fetch page module 123" in caplog.text


def test_page_http_failure_is_counted(caplog):
    ctx = run_page_handler(FakeResponse(status_code=503))

    assert ctx.stats.failed == 1
    assert "Page module 123 returned status 503" in caplog.text


def h5p_package(content: str) -> bytes:
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("content/content.json", content)
    return package.getvalue()


def run_h5p_handler(monkeypatch, response, package_metadata=None):
    ctx = make_context()
    session = FakeSession()
    session.add("GET", H5P_PACKAGE_URL, response)
    ctx.session = session
    package_file = {"fileurl": H5P_PACKAGE_URL, **(package_metadata or {})}
    monkeypatch.setattr(
        sync_handlers.moodle_api,
        "get_h5pactivities_by_course",
        lambda session, wstoken, course_id: [
            {
                "coursemodule": 317,
                "package": [package_file],
            }
        ],
    )
    course_node = Node("Course", 1, "Course", None)
    section_node = course_node.add_child("Section", 2, "Section")
    assert section_node is not None
    module_context = sync_handlers.ModuleContext(
        ctx, 1, course_node, section_node, {}, {}
    )

    sync_handlers.handle_embedded_link_module(
        module_context,
        {"id": 317, "modname": "h5pactivity", "name": "Interactive video"},
    )

    return section_node


def range_package_response(package, requested_ranges, bytes_served):
    def respond(url, kwargs):
        del url
        headers = kwargs.get("headers", {})
        assert headers.get("Accept-Encoding") == "identity"
        range_header = headers.get("Range")
        requested_ranges.append(range_header)
        if range_header is None:
            return FakeResponse(
                content=package,
                headers={"Content-Length": str(len(package))},
            )
        unit, bounds = range_header.split("=", 1)
        start_text, end_text = bounds.split("-", 1)
        assert unit == "bytes"
        start, end = int(start_text), int(end_text)
        body = package[start : end + 1]
        bytes_served.append(len(body))
        return FakeResponse(
            content=body,
            status_code=206,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{end}/{len(package)}",
            },
        )

    return respond


def run_cached_h5p_handler(
    monkeypatch,
    tmp_path,
    content_hash,
    response=None,
    *,
    timemodified=None,
):
    ctx = make_context({"paths.sync_directory": str(tmp_path)})
    session = FakeSession()
    if response is not None:
        session.add("GET", H5P_PACKAGE_URL, response)
    ctx.session = session
    monkeypatch.setattr(
        sync_handlers.moodle_api,
        "get_h5pactivities_by_course",
        lambda session, wstoken, course_id: [
            {
                "coursemodule": 317,
                "contenthash": content_hash,
                "timemodified": timemodified,
                "package": [{"fileurl": H5P_PACKAGE_URL}],
            }
        ],
    )
    root = Node("", -1, "Root", None)
    semester_node = root.add_child("26ss", None, "Semester")
    assert semester_node is not None
    course_node = semester_node.add_child("Course", 1, "Course")
    assert course_node is not None
    section_node = course_node.add_child("Section", 2, "Section")
    assert section_node is not None
    ctx.root_node = root
    module_context = sync_handlers.ModuleContext(
        ctx, 1, course_node, section_node, {}, {}
    )

    sync_handlers.handle_embedded_link_module(
        module_context,
        {"id": 317, "modname": "h5pactivity", "name": "Interactive video"},
    )

    return ctx, section_node, session


def run_cached_page_handler(tmp_path, timemodified, response=None):
    ctx = make_context(
        {
            "paths.sync_directory": str(tmp_path),
            "links.youtube": True,
            "links.opencast": False,
            "links.sciebo": False,
        }
    )
    session = FakeSession()
    if response is not None:
        session.add("GET", PAGE_CONTENT_URL, response)
    ctx.session = session
    root = Node("", -1, "Root", None)
    semester_node = root.add_child("26ss", None, "Semester")
    assert semester_node is not None
    course_node = semester_node.add_child("Course", 1, "Course")
    assert course_node is not None
    section_node = course_node.add_child("Section", 2, "Section")
    assert section_node is not None
    ctx.root_node = root
    module_context = sync_handlers.ModuleContext(
        ctx, 1, course_node, section_node, {}, {}
    )
    module = {
        "id": 315,
        "modname": "page",
        "name": "Page",
        "contents": [
            {
                "filename": "index.html",
                "fileurl": PAGE_CONTENT_URL,
                "timemodified": timemodified,
            }
        ],
    }

    sync_handlers.handle_embedded_link_module(module_context, module)

    return ctx, section_node, session


def test_h5p_package_download_is_streamed_and_capped(monkeypatch, caplog):
    monkeypatch.setattr(sync_handlers, "H5P_PACKAGE_MAX_BYTES", 8)

    def package_response(url, kwargs):
        assert kwargs == {"stream": True, "timeout": HTTP_TIMEOUT_SECONDS}
        return FakeResponse(chunks=[b"1234", b"56789"])

    section_node = run_h5p_handler(monkeypatch, package_response)

    assert section_node.children == []
    assert "H5P package for module 317 is too large" in caplog.text


def test_h5p_content_is_bounded_after_decompression(monkeypatch, caplog):
    monkeypatch.setattr(sync_handlers, "H5P_CONTENT_MAX_BYTES", 32)
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "content/content.json",
            "https://www.youtube.com/watch?v=abcdefghijk",
        )
    package_bytes = package.getvalue()
    requested_ranges = []
    bytes_served = []

    section_node = run_h5p_handler(
        monkeypatch,
        range_package_response(package_bytes, requested_ranges, bytes_served),
        {"filesize": len(package_bytes)},
    )

    assert section_node.children == []
    assert requested_ranges and None not in requested_ranges
    assert "H5P content for module 317 is too large" in caplog.text


def test_h5p_content_is_extracted_with_bounded_range_requests(monkeypatch):
    video_url = "https://www.youtube.com/watch?v=abcdefghijk"
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("content/video.mp4", b"x" * 1024**2)
        archive.writestr("content/content.json", video_url)
    package_bytes = package.getvalue()
    requested_ranges = []
    bytes_served = []

    section_node = run_h5p_handler(
        monkeypatch,
        range_package_response(package_bytes, requested_ranges, bytes_served),
        {"filesize": len(package_bytes)},
    )

    assert [child.url for child in section_node.children] == [video_url]
    assert requested_ranges and None not in requested_ranges
    assert sum(bytes_served) < len(package_bytes) // 10


def test_h5p_range_requests_fall_back_when_the_server_ignores_them(monkeypatch):
    video_url = "https://www.youtube.com/watch?v=abcdefghijk"
    package = h5p_package(video_url)
    requested_ranges = []

    def ignore_range(url, kwargs):
        del url
        requested_ranges.append(kwargs.get("headers", {}).get("Range"))
        return FakeResponse(
            content=package,
            headers={"Content-Length": str(len(package))},
        )

    section_node = run_h5p_handler(
        monkeypatch,
        ignore_range,
        {"filesize": len(package)},
    )

    assert [child.url for child in section_node.children] == [video_url]
    assert len(requested_ranges) == 2
    assert requested_ranges[0] is not None
    assert requested_ranges[1] is None


def test_large_h5p_package_is_spooled_and_inspected(monkeypatch):
    monkeypatch.setattr(sync_handlers, "H5P_PACKAGE_MEMORY_BYTES", 64)
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "content/content.json",
            "https://www.youtube.com/watch?v=abcdefghijk",
        )
        archive.writestr("content/video.mp4", b"x" * 1024)

    section_node = run_h5p_handler(
        monkeypatch,
        FakeResponse(
            content=package.getvalue(),
            headers={"Content-Length": str(125 * 1024**2)},
        ),
    )

    assert len(section_node.children) == 1
    assert section_node.children[0].url == (
        "https://www.youtube.com/watch?v=abcdefghijk"
    )


def test_h5p_content_cache_reuses_unchanged_and_refreshes_changed_packages(
    monkeypatch, tmp_path
):
    first_url = "https://www.youtube.com/watch?v=abcdefghijk"
    first, first_section, first_session = run_cached_h5p_handler(
        monkeypatch,
        tmp_path,
        "a" * 40,
        FakeResponse(content=h5p_package(first_url)),
    )
    course_cache.cache_root_node(first)

    _, cached_section, cached_session = run_cached_h5p_handler(
        monkeypatch,
        tmp_path,
        "a" * 40,
    )

    assert first_session.count("GET", H5P_PACKAGE_URL) == 1
    assert cached_session.count("GET", H5P_PACKAGE_URL) == 0
    assert [child.url for child in first_section.children] == [first_url]
    assert [child.url for child in cached_section.children] == [first_url]

    changed_url = "https://www.youtube.com/watch?v=zyxwvutsrqp"
    changed, changed_section, changed_session = run_cached_h5p_handler(
        monkeypatch,
        tmp_path,
        "b" * 40,
        FakeResponse(content=h5p_package(changed_url)),
    )
    course_cache.cache_root_node(changed)

    assert changed_session.count("GET", H5P_PACKAGE_URL) == 1
    assert [child.url for child in changed_section.children] == [changed_url]

    _, refreshed_section, refreshed_session = run_cached_h5p_handler(
        monkeypatch,
        tmp_path,
        "b" * 40,
    )

    assert refreshed_session.count("GET", H5P_PACKAGE_URL) == 0
    assert [child.url for child in refreshed_section.children] == [changed_url]


def test_h5p_timestamp_marker_is_used_when_content_hash_is_missing(
    monkeypatch, tmp_path
):
    video_url = "https://www.youtube.com/watch?v=abcdefghijk"
    first, _, _ = run_cached_h5p_handler(
        monkeypatch,
        tmp_path,
        None,
        FakeResponse(content=h5p_package(video_url)),
        timemodified=1234,
    )
    course_cache.cache_root_node(first)

    _, cached_section, cached_session = run_cached_h5p_handler(
        monkeypatch,
        tmp_path,
        None,
        timemodified=1234,
    )

    assert cached_session.count("GET", H5P_PACKAGE_URL) == 0
    assert [child.url for child in cached_section.children] == [video_url]


def test_h5p_without_a_revision_marker_is_downloaded_each_run(monkeypatch, tmp_path):
    video_url = "https://www.youtube.com/watch?v=abcdefghijk"
    first, _, first_session = run_cached_h5p_handler(
        monkeypatch,
        tmp_path,
        None,
        FakeResponse(content=h5p_package(video_url)),
    )
    course_cache.cache_root_node(first)

    _, _, second_session = run_cached_h5p_handler(
        monkeypatch,
        tmp_path,
        None,
        FakeResponse(content=h5p_package(video_url)),
    )

    assert first_session.count("GET", H5P_PACKAGE_URL) == 1
    assert second_session.count("GET", H5P_PACKAGE_URL) == 1


def test_page_content_cache_reuses_unchanged_and_refreshes_changed_pages(tmp_path):
    first_url = "https://www.youtube.com/watch?v=abcdefghijk"
    first, first_section, first_session = run_cached_page_handler(
        tmp_path,
        100,
        FakeResponse(text=f'<a href="{first_url}">first</a>'),
    )
    course_cache.cache_root_node(first)

    cached, cached_section, cached_session = run_cached_page_handler(tmp_path, 100)
    course_cache.cache_root_node(cached)

    assert first_session.count("GET", PAGE_CONTENT_URL) == 1
    assert cached_session.count("GET", PAGE_CONTENT_URL) == 0
    assert [child.url for child in first_section.children] == [first_url]
    assert [child.url for child in cached_section.children] == [first_url]

    changed_url = "https://www.youtube.com/watch?v=zyxwvutsrqp"
    _, changed_section, changed_session = run_cached_page_handler(
        tmp_path,
        101,
        FakeResponse(text=f'<a href="{changed_url}">changed</a>'),
    )

    assert changed_session.count("GET", PAGE_CONTENT_URL) == 1
    assert [child.url for child in changed_section.children] == [changed_url]


def test_page_without_revision_marker_is_fetched_each_run(tmp_path):
    page = FakeResponse(
        text='<a href="https://www.youtube.com/watch?v=abcdefghijk">video</a>'
    )
    first, _, first_session = run_cached_page_handler(tmp_path, None, page)
    course_cache.cache_root_node(first)

    _, _, second_session = run_cached_page_handler(tmp_path, None, page)

    assert first_session.count("GET", PAGE_CONTENT_URL) == 1
    assert second_session.count("GET", PAGE_CONTENT_URL) == 1


def test_update_feed_reuses_and_refreshes_assignment_and_quiz_data(
    monkeypatch, tmp_path
):
    courses = [
        {
            "id": 901,
            "shortname": "Cached Course",
            "idnumber": "26ss-course",
        }
    ]
    course = [
        {
            "id": 902,
            "name": "General",
            "modules": [
                {"id": 42, "instance": 7, "modname": "assign", "name": "Assignment"},
                {"id": 43, "instance": 8, "modname": "quiz", "name": "Quiz"},
            ],
        }
    ]
    assignments = {
        "assignments": [
            {
                "id": 7,
                "cmid": 42,
                "intro": "",
                "introattachments": [],
                "teamsubmission": 0,
            }
        ]
    }
    install_moodle_fixtures(
        monkeypatch,
        courses,
        {901: course},
        {901: assignments},
    )
    state = {"version": 1, "changed": frozenset()}
    calls = {"submission": 0, "attempts": 0, "review": 0}
    update_calls = []

    def submission_files(session, wstoken, user_id, assignment_id):
        calls["submission"] += 1
        version = state["version"]
        return [
            {
                "filename": f"feedback-v{version}.pdf",
                "filepath": "/",
                "fileurl": (
                    "https://moodle.rwth-aachen.de/pluginfile.php/1/"
                    f"mod_assign/feedback/feedback-v{version}.pdf"
                ),
                "timemodified": 100 + int(version),
            }
        ]

    def quiz_attempts(session, wstoken, quiz_id):
        calls["attempts"] += 1
        return [{"id": 5, "timefinish": 1}]

    def quiz_review(session, wstoken, attempt_id):
        calls["review"] += 1
        return {"questions": [{"html": f"<p>Review v{state['version']}</p>"}]}

    def course_updates(session, wstoken, course_id, module_since, log):
        update_calls.append(dict(module_since))
        return moodle.CourseUpdates(dict(module_since), state["changed"], frozenset())

    monkeypatch.setattr(moodle, "get_assignment_submission_files", submission_files)
    monkeypatch.setattr(moodle, "get_quiz_attempts", quiz_attempts)
    monkeypatch.setattr(moodle, "get_quiz_attempt_review", quiz_review)
    monkeypatch.setattr(moodle, "check_course_updates", course_updates)
    monkeypatch.setattr(
        moodle,
        "get_quizzes_by_course",
        lambda session, wstoken, course_id: [
            {"coursemodule": 43, "id": 8, "timeclose": 10_000}
        ],
    )

    def run_at(watermark, user_id=10001):
        context = make_context(
            {
                "paths.sync_directory": str(tmp_path),
                "modules.assignment": True,
                "modules.quiz": "html",
            }
        )
        context.moodle_account = MoodleAccount(
            MoodleTokens(
                "fake-user",
                "fake-webservice-token",
                "fake-private-token",
                moodle_user_id=user_id,
            )
        )
        context.session = FakeSession()
        context.moodle_functions = frozenset({moodle.MOODLE_UPDATE_FUNCTION})
        context.moodle_server_time = watermark + 5
        sync.sync(context)
        return context

    first = run_at(200)
    course_cache.cache_root_node(first)
    second = run_at(300)

    assert update_calls == [{42: 200, 43: 200}]
    assert calls == {"submission": 1, "attempts": 1, "review": 1}
    node_at_path(
        second.root_node,
        [
            "26ss",
            "Cached Course",
            "General",
            "Assignment",
            "feedback-v1.pdf",
        ],
    )
    review_url = "https://moodle.rwth-aachen.de/mod/quiz/review.php?attempt=5"
    assert "Review v1" in second.quiz_review_cache[review_url]
    course_cache.cache_root_node(second)

    # Assignment submissions and quiz attempts are private to the Moodle user.
    # A second account using the same sync directory must not reuse them.
    other_user = run_at(350, user_id=20002)

    assert update_calls == [{42: 200, 43: 200}]
    assert calls == {"submission": 2, "attempts": 2, "review": 2}
    assert "Review v1" in other_user.quiz_review_cache[review_url]

    state["version"] = 2
    state["changed"] = frozenset({42, 43})
    third = run_at(400)

    assert update_calls == [{42: 200, 43: 200}, {42: 300, 43: 300}]
    assert calls == {"submission": 3, "attempts": 3, "review": 3}
    node_at_path(
        third.root_node,
        [
            "26ss",
            "Cached Course",
            "General",
            "Assignment",
            "feedback-v2.pdf",
        ],
    )
    assert "Review v2" in third.quiz_review_cache[review_url]

    # Moodle's update callback does not reliably cover a teammate changing the
    # shared group submission, so those assignments must keep using live data.
    course_cache.cache_root_node(third)
    assignments["assignments"][0]["teamsubmission"] = 1
    state["version"] = 3
    state["changed"] = frozenset()
    team_run = run_at(500)

    assert update_calls == [
        {42: 200, 43: 200},
        {42: 300, 43: 300},
        {42: 400, 43: 400},
    ]
    assert calls == {"submission": 4, "attempts": 3, "review": 3}
    node_at_path(
        team_run.root_node,
        [
            "26ss",
            "Cached Course",
            "General",
            "Assignment",
            "feedback-v3.pdf",
        ],
    )


def test_quiz_cache_refreshes_at_review_phase_boundary(monkeypatch, tmp_path):
    calls = {"attempts": 0, "review": 0}
    quiz_state = {"timeclose": 0}

    monkeypatch.setattr(
        moodle,
        "get_quizzes_by_course",
        lambda session, wstoken, course_id: [
            {"coursemodule": 43, "id": 8, "timeclose": quiz_state["timeclose"]}
        ],
    )

    def quiz_attempts(session, wstoken, quiz_id):
        calls["attempts"] += 1
        return [{"id": 5, "timefinish": 50}]

    def quiz_review(session, wstoken, attempt_id):
        calls["review"] += 1
        return {"questions": [{"html": f"<p>Review {calls['review']}</p>"}]}

    monkeypatch.setattr(moodle, "get_quiz_attempts", quiz_attempts)
    monkeypatch.setattr(moodle, "get_quiz_attempt_review", quiz_review)

    def run_at(server_time, updates):
        context = make_context(
            {
                "paths.sync_directory": str(tmp_path),
                "modules.quiz": "html",
            }
        )
        context.session = FakeSession()
        context.moodle_server_time = server_time
        root = Node("", -1, "Root", None)
        semester = root.add_child("26ss", None, "Semester")
        course_node = semester.add_child("Cached Course", 901, "Course")
        section_node = course_node.add_child("General", 902, "Section")
        context.root_node = root
        module_context = sync_handlers.ModuleContext(
            context,
            901,
            course_node,
            section_node,
            {},
            {},
            course_updates=updates,
        )
        sync_handlers.handle_quiz_module(
            module_context,
            {"id": 43, "instance": 8, "modname": "quiz", "name": "Quiz"},
        )
        return context

    first = run_at(100, None)
    course_cache.cache_root_node(first)

    unchanged = moodle.CourseUpdates({43: 95}, frozenset(), frozenset())
    before_boundary = run_at(160, unchanged)
    review_url = "https://moodle.rwth-aachen.de/mod/quiz/review.php?attempt=5"

    assert calls == {"attempts": 1, "review": 1}
    assert "Review 1" in before_boundary.quiz_review_cache[review_url]

    at_boundary = run_at(170, unchanged)

    assert calls == {"attempts": 2, "review": 2}
    assert "Review 2" in at_boundary.quiz_review_cache[review_url]

    course_cache.cache_root_node(at_boundary)
    quiz_state["timeclose"] = 160
    after_override_change = run_at(
        180,
        moodle.CourseUpdates({43: 165}, frozenset(), frozenset()),
    )

    assert calls == {"attempts": 3, "review": 3}
    assert "Review 3" in after_override_change.quiz_review_cache[review_url]


def test_failed_course_update_check_does_not_disable_later_courses(monkeypatch, caplog):
    courses = [
        {"id": 901, "shortname": "First", "idnumber": "26ss-first"},
        {"id": 902, "shortname": "Second", "idnumber": "26ss-second"},
    ]
    course_contents = {
        course_id: [
            {
                "id": 1000 + course_id,
                "name": "General",
                "modules": [
                    {
                        "id": course_id,
                        "instance": course_id,
                        "modname": "assign",
                        "name": "Assignment",
                    },
                    {
                        "id": course_id + 10_000,
                        "instance": course_id + 10_000,
                        "modname": "hsuforum",
                        "name": "Open Forum",
                    },
                ],
            }
        ]
        for course_id in (901, 902)
    }
    install_moodle_fixtures(monkeypatch, courses, course_contents)
    monkeypatch.setattr(
        course_cache,
        "get_assignment_cache_entry",
        lambda ctx, course_node, module_id, log: course_cache.AssignmentCacheEntry(
            100, []
        ),
    )
    update_calls = []

    def course_update(session, wstoken, course_id, module_since, log):
        update_calls.append((course_id, dict(module_since)))
        if course_id == 901:
            return None
        return moodle.CourseUpdates(dict(module_since), frozenset(), frozenset())

    monkeypatch.setattr(moodle, "check_course_updates", course_update)
    context = make_context({"modules.assignment": True})
    context.session = FakeSession()
    context.moodle_functions = frozenset({moodle.MOODLE_UPDATE_FUNCTION})
    caplog.set_level(logging.INFO, logger="syncmymoodle.sync")

    sync.sync(context)

    assert update_calls == [(901, {901: 100}), (902, {902: 100})]
    assert moodle.MOODLE_UPDATE_FUNCTION in context.moodle_functions
    assert (
        caplog.messages.count(
            "Moodle incremental update check failed for First; using full module "
            "queries for this course"
        )
        == 1
    )


def test_nested_moodle_folder_paths_are_preserved(monkeypatch):
    courses = [load_json_fixture("moodle", "courses.json")[0]]
    syncer = make_context()
    install_moodle_fixtures(
        monkeypatch,
        courses,
        {101: load_json_fixture("moodle", "nested_folder_course.json")},
    )
    syncer.session = FakeSession()

    sync.sync(syncer)

    assert_snapshot("nested_folder_tree.txt", node_rows(syncer.root_node))


def test_assignment_opencast_metadata_is_refreshed_between_runs(
    monkeypatch,
    tmp_path,
):
    courses = [load_json_fixture("moodle", "courses.json")[1]]
    config = {
        "paths.sync_directory": str(tmp_path),
        "modules.assignment": True,
        "modules.resource": False,
        "modules.folder": False,
        "links.youtube": False,
        "links.opencast": True,
        "links.sciebo": False,
    }
    install_moodle_fixtures(
        monkeypatch,
        courses,
        {102: load_json_fixture("moodle", "assignment_opencast_course.json")},
        {102: load_json_fixture("moodle", "assignment_opencast_assignments.json")},
    )

    authenticated = []
    resolved = []
    monkeypatch.setattr(
        opencast,
        "authorize_course_for_episode",
        lambda ctx, course_id, episode_id, *a, **k: (
            authenticated.append((course_id, episode_id)) or True
        ),
    )
    episode_id = "11111111-2222-4333-8444-555555555555"
    series_id = "series-1111-2222"

    def fetch_result_list(ctx, url, context, log):
        resolved.append(url)
        refreshed = len(resolved) == 2
        return [
            opencast_episode_entry(
                episode_id,
                "https://video.example.test/"
                f"{episode_id}/presentation.mp4"
                + ("?signature=refreshed" if refreshed else ""),
                series_id=series_id,
                checksum="2" * 32 if refreshed else "1" * 32,
            )
        ]

    monkeypatch.setattr(opencast, "fetch_result_list", fetch_result_list)

    first = make_context(config)
    first.session = FakeSession()
    sync.sync(first)
    course_cache.cache_root_node(first)

    second = make_context(config)
    second.session = FakeSession()
    sync.sync(second)

    assert authenticated == [(102, episode_id), (102, episode_id)]
    assert resolved == [
        f"{opencast.OPENCAST_SEARCH_URL}?id={episode_id}",
        f"{opencast.OPENCAST_SEARCH_URL}?limit=100&offset=0&sid={series_id}",
    ]
    assert_snapshot("assignment_opencast_tree.txt", node_rows(first.root_node))
    assert node_rows(second.root_node) != node_rows(first.root_node)
    assert any(
        "?signature=refreshed" in row and "2" * 32 in row
        for row in node_rows(second.root_node)
    )


def test_skip_rules_apply_to_sections_modules_links_and_domains(monkeypatch):
    courses = [load_json_fixture("moodle", "courses.json")[2]]
    syncer = make_context(
        {
            "filters.exclude_sections": {"*": ["Hidden*"]},
            "filters.exclude_modules": {"103": ["Skip Module"]},
            "filters.exclude_links": ["*excluded.pdf"],
            "filters.allowed_domains": ["moodle.rwth-aachen.de"],
            "modules.assignment": False,
            "modules.resource": True,
            "modules.folder": False,
            "links.youtube": False,
            "links.opencast": False,
            "links.sciebo": False,
        }
    )
    install_moodle_fixtures(
        monkeypatch,
        courses,
        {103: load_json_fixture("moodle", "skip_rules_course.json")},
    )
    syncer.session = FakeSession()

    sync.sync(syncer)

    assert_snapshot("skip_rules_tree.txt", node_rows(syncer.root_node))


def test_sciebo_public_share_is_cached_per_sync_run(caplog):
    link = "https://rwth-aachen.sciebo.de/s/share-token-123"
    public_root = "https://rwth-aachen.sciebo.de/public.php/webdav/"
    public_slides = "https://rwth-aachen.sciebo.de/public.php/webdav/slides/"
    syncer = make_context(
        {
            "modules.assignment": False,
            "modules.resource": False,
            "modules.folder": False,
            "links.youtube": False,
            "links.opencast": False,
            "links.sciebo": True,
        }
    )
    session = FakeSession()
    session.add(
        "GET", link, FakeResponse(text=load_fixture("sciebo", "public_share.html"))
    )
    session.add(
        "PROPFIND",
        public_root,
        FakeResponse(text=load_fixture("sciebo", "propfind_root.xml")),
    )
    session.add(
        "PROPFIND",
        public_slides,
        FakeResponse(text=load_fixture("sciebo", "propfind_slides.xml")),
    )
    syncer.session = session
    caplog.set_level(logging.INFO, logger="syncmymoodle.sciebo")

    root = Node("", -1, "Root", None)
    first_parent = root.add_child("First occurrence", 1, "Section")
    second_parent = root.add_child("Second occurrence", 2, "Section")

    links.scan_for_links(syncer, link, first_parent, 101)
    links.scan_for_links(syncer, link, second_parent, 101)

    assert session.count("GET", link) == 1
    assert session.count("PROPFIND", public_root) == 1
    assert session.count("PROPFIND", public_slides) == 1
    sciebo_root = first_parent.children[0]
    assert sciebo_root.children[0].remote_size == 123
    assert sciebo_root.children[1].children[0].remote_size == 456
    assert [
        row.replace("First occurrence/", "") for row in node_rows(first_parent)
    ] == [row.replace("Second occurrence/", "") for row in node_rows(second_parent)]
    assert node_rows(first_parent) == [
        "Sciebo Folder | First occurrence/sciebo-share-token-123 |  |  |",
        "Sciebo File | First occurrence/sciebo-share-token-123/readme.pdf | "
        "https://rwth-aachen.sciebo.de/public.php/webdav/readme.pdf |  | "
        "1111111111111111111111111111111111111111",
        "Sciebo Folder | First occurrence/sciebo-share-token-123/slides |  |  | "
        '"folder-slides"',
        "Sciebo File | First occurrence/sciebo-share-token-123/slides/deck.pdf | "
        "https://rwth-aachen.sciebo.de/public.php/webdav/slides/deck.pdf |  | "
        "2222222222222222222222222222222222222222",
    ]
    assert "fake-request-token" not in caplog.text
    assert "Sciebo sharingToken:" not in caplog.text


def test_opencast_error_response_body_is_not_logged(caplog):
    url = "https://engage.streaming.rwth-aachen.de/search/private.json"
    syncer = make_context()
    session = FakeSession()
    session.add(
        "GET",
        url,
        FakeResponse(status_code=500, text="private-opencast-response"),
    )
    syncer.session = session
    caplog.set_level(logging.INFO, logger="syncmymoodle.opencast")

    assert opencast.fetch_result_list(syncer, url, "episode") is None

    assert "private-opencast-response" not in caplog.text


def test_opencast_lti_authorization_is_reused_for_the_course():
    syncer = make_context()
    syncer.session = FakeSession()
    syncer.browser_session = FakeSession()
    syncer.browser_session_key = "browser-session-key"
    episodes = (
        (101, "11111111-2222-4333-8444-555555555555"),
        (101, "22222222-3333-4444-8555-666666666666"),
        (102, "33333333-4444-4555-8666-777777777777"),
    )

    def launch_url(course_id, episode_id):
        query = urllib.parse.urlencode(
            {
                "courseid": course_id,
                "episodeid": episode_id,
                "sesskey": syncer.browser_session_key,
                "ocinstanceid": 1,
            }
        )
        return f"{opencast.MOODLE_URL}filter/opencast/ltilaunch.php?{query}"

    form = FakeResponse(text='<input name="oauth_consumer_key" value="key">')
    syncer.browser_session.add("GET", launch_url(*episodes[0]), form)
    syncer.browser_session.add("GET", launch_url(*episodes[2]), form)
    syncer.session.add("POST", opencast.OPENCAST_LTI_URL, FakeResponse(text="ok"))

    assert opencast.authorize_course_for_episode(syncer, *episodes[0])
    assert opencast.authorize_course_for_episode(syncer, *episodes[1])
    assert opencast.authorize_course_for_episode(syncer, *episodes[2])

    assert syncer.browser_session.count("GET") == 2
    assert syncer.session.count("POST", opencast.OPENCAST_LTI_URL) == 2


def test_opencast_repeated_503_opens_shared_service_circuit(caplog):
    url = "https://engage.streaming.rwth-aachen.de/search/episode.json"
    syncer = make_context()
    session = FakeSession()
    session.add("GET", url, FakeResponse(status_code=503))
    syncer.session = session
    caplog.set_level(logging.WARNING, logger="syncmymoodle.opencast")

    for _ in range(4):
        assert opencast.fetch_result_list(syncer, url, "episode") is None

    assert session.count("GET", url) == 3
    assert [
        message
        for message in caplog.messages
        if message.startswith("Opencast unavailable")
    ] == [
        "Opencast unavailable after 3 consecutive transient failures: episode from "
        f"{url} returned HTTP 503; skipping remaining requests for this sync. "
        "Check the RWTH ITC status page: "
        f"{opencast.RWTH_MOODLE_STATUS_URL}"
    ]


def test_opencast_malformed_results_open_shared_service_circuit(caplog):
    url = "https://engage.streaming.rwth-aachen.de/search/episode.json"
    syncer = make_context()
    session = FakeSession()
    session.add("GET", url, FakeResponse(json_payload={}))
    syncer.session = session
    caplog.set_level(logging.WARNING, logger="syncmymoodle.opencast")

    for _ in range(3):
        assert opencast.fetch_result_list(syncer, url, "episode") is None
    assert opencast.fetch_result_list(syncer, url, "episode") is None

    assert session.count("GET", url) == 3
    assert caplog.messages[-1] == (
        "Opencast unavailable after 3 consecutive transient failures: episode "
        "response did not contain a result list; skipping remaining requests for "
        "this sync. Check the RWTH ITC status page: "
        f"{opencast.RWTH_MOODLE_STATUS_URL}"
    )


def test_opencast_track_name_does_not_depend_on_sibling_count(monkeypatch):
    syncer = make_context()
    presenter = opencast.OpencastTrack(
        "https://video.example.test/presenter.mp4",
        flavor_type="presenter",
    )
    presentation = opencast.OpencastTrack(
        "https://video.example.test/presentation.mp4",
        flavor_type="presentation",
    )
    resolved_tracks = (presenter,)
    monkeypatch.setattr(
        opencast,
        "resolve_tracks_from_episode",
        lambda *args, **kwargs: resolved_tracks,
    )

    single_parent = Node("Single", 1, "Section", None)
    opencast.add_episode_nodes(syncer, single_parent, "Lecture.mp4", "episode")
    resolved_tracks = (presentation, presenter)
    multi_parent = Node("Multiple", 2, "Section", None)
    opencast.add_episode_nodes(syncer, multi_parent, "Lecture.mp4", "episode")

    single_name = single_parent.children[0].name
    multi_name = next(
        child.name for child in multi_parent.children if child.url == presenter.url
    )
    assert single_name == multi_name == "Lecture (presenter).mp4"


def test_opencast_keeps_distinct_tracks_without_flavor_metadata(monkeypatch):
    episode_id = "untyped-episode"
    urls = [
        "https://video.example.test/camera-a.mp4",
        "https://video.example.test/camera-b.mp4",
    ]
    syncer = make_context()
    monkeypatch.setattr(
        opencast,
        "fetch_result_list",
        lambda *args, **kwargs: [
            {
                "mediapackage": {
                    "media": {
                        "track": [
                            {
                                "mimetype": "video/mp4",
                                "url": url,
                                "video": {"resolution": resolution},
                            }
                            for url, resolution in zip(
                                urls, ["1280x720", "1920x1080"], strict=True
                            )
                        ]
                    }
                }
            }
        ],
    )

    tracks = opencast.resolve_tracks_from_episode(syncer, episode_id)

    assert tracks is not None
    assert [track.url for track in tracks] == urls
    assert all(track.flavor_type is None for track in tracks)

    parent = Node("Section", 1, "Section", None)
    opencast.add_episode_nodes(syncer, parent, "Lecture.mp4", episode_id)
    assert len({child.name for child in parent.children}) == 2
    assert all(child.name.startswith("Lecture (video-") for child in parent.children)


def test_sharing_token_from_link_extracts_url_segment():
    assert (
        sciebo.sharing_token_from_link("https://rwth-aachen.sciebo.de/s/AbC123")
        == "AbC123"
    )
    # Trailing slashes and query strings must not leak into the token.
    assert (
        sciebo.sharing_token_from_link("https://rwth-aachen.sciebo.de/s/AbC123/?x=1")
        == "AbC123"
    )


def test_sciebo_share_without_token_input_uses_url_token():
    # Newer share pages drop the <input name="sharingToken">; the token is then
    # derived from the /s/<token> URL segment so the share still resolves.
    link = "https://rwth-aachen.sciebo.de/s/share-token-123"
    public_root = "https://rwth-aachen.sciebo.de/public.php/webdav/"
    public_slides = "https://rwth-aachen.sciebo.de/public.php/webdav/slides/"
    syncer = make_context(
        {
            "modules.assignment": False,
            "modules.resource": False,
            "modules.folder": False,
            "links.youtube": False,
            "links.opencast": False,
            "links.sciebo": True,
        }
    )
    session = FakeSession()
    session.add(
        "GET",
        link,
        FakeResponse(text=load_fixture("sciebo", "public_share_no_token.html")),
    )
    session.add(
        "PROPFIND",
        public_root,
        FakeResponse(text=load_fixture("sciebo", "propfind_root.xml")),
    )
    session.add(
        "PROPFIND",
        public_slides,
        FakeResponse(text=load_fixture("sciebo", "propfind_slides.xml")),
    )
    syncer.session = session

    root = Node("", -1, "Root", None)
    parent = root.add_child("Section", 1, "Section")
    links.scan_for_links(syncer, link, parent, 101)

    assert session.count("PROPFIND", public_root) == 1
    assert node_rows(parent) == [
        "Sciebo Folder | Section/sciebo-share-token-123 |  |  |",
        "Sciebo File | Section/sciebo-share-token-123/readme.pdf | "
        "https://rwth-aachen.sciebo.de/public.php/webdav/readme.pdf |  | "
        "1111111111111111111111111111111111111111",
        'Sciebo Folder | Section/sciebo-share-token-123/slides |  |  | "folder-slides"',
        "Sciebo File | Section/sciebo-share-token-123/slides/deck.pdf | "
        "https://rwth-aachen.sciebo.de/public.php/webdav/slides/deck.pdf |  | "
        "2222222222222222222222222222222222222222",
    ]


def _assert_sciebo_share_outage(
    caplog, response_factory, scan_share, logger_name, reason
):
    share_links = [
        f"https://rwth-aachen.sciebo.de/s/share-{index}" for index in range(4)
    ]
    syncer = make_context({"links.sciebo": True})
    session = FakeSession()
    for link in share_links[:3]:
        session.add("GET", link, response_factory())
    syncer.session = session
    parent = Node("Section", 1, "Section", None)
    caplog.set_level(logging.WARNING, logger=logger_name)

    for link in share_links:
        scan_share(syncer, link, parent)

    assert session.calls == [("GET", link) for link in share_links[:3]]
    assert caplog.messages == [
        f"Sciebo transient failure: {reason}",
        f"Sciebo transient failure: {reason}",
        f"Sciebo unavailable after 3 consecutive transient failures: {reason}; "
        "skipping remaining requests for this sync. Check the RWTH ITC status page: "
        f"{sciebo.RWTH_SCIEBO_STATUS_URL}",
    ]
    assert parent.children == []
    assert syncer.service_outages.should_skip(sciebo.SCIEBO_URL)


def test_sciebo_503_opens_shared_service_circuit(caplog):
    _assert_sciebo_share_outage(
        caplog,
        lambda: FakeResponse(status_code=503),
        lambda syncer, link, parent: links.scan_for_links(
            syncer, link, parent, 101, single=True
        ),
        "syncmymoodle.links",
        "share page returned HTTP 503",
    )


def test_sciebo_200_maintenance_pages_open_shared_service_circuit(caplog):
    _assert_sciebo_share_outage(
        caplog,
        lambda: FakeResponse(text="<html><head></head><body>maintenance</body></html>"),
        sciebo.scan_public_shares,
        "syncmymoodle.sciebo",
        "share page returned an unexpected response without a request token",
    )


def test_sciebo_200_html_webdav_responses_do_not_cache_empty_shares(caplog):
    share_links = [
        f"https://rwth-aachen.sciebo.de/s/share-{index}" for index in range(4)
    ]
    public_root = "https://rwth-aachen.sciebo.de/public.php/webdav/"
    syncer = make_context({"links.sciebo": True})
    session = FakeSession()
    for link in share_links[:3]:
        session.add(
            "GET",
            link,
            FakeResponse(text=load_fixture("sciebo", "public_share.html")),
        )
    session.add(
        "PROPFIND",
        public_root,
        FakeResponse(text="<html><body>maintenance</body></html>"),
    )
    syncer.session = session
    parent = Node("Section", 1, "Section", None)
    caplog.set_level(logging.WARNING, logger="syncmymoodle.sciebo")

    for link in share_links:
        sciebo.scan_public_shares(syncer, link, parent)

    assert session.calls == [
        call
        for link in share_links[:3]
        for call in (("GET", link), ("PROPFIND", public_root))
    ]
    assert caplog.messages == [
        "Sciebo transient failure: WebDAV returned an unexpected response instead "
        "of a DAV listing",
        "Sciebo transient failure: WebDAV returned an unexpected response instead "
        "of a DAV listing",
        "Sciebo unavailable after 3 consecutive transient failures: WebDAV returned "
        "an unexpected response instead of a DAV listing; skipping remaining "
        "requests for this sync. Check the RWTH ITC status page: "
        f"{sciebo.RWTH_SCIEBO_STATUS_URL}",
    ]
    assert parent.children == []
    assert not any(syncer.sciebo_link_cache.values())


def test_sciebo_webdav_503_does_not_cache_an_empty_share(caplog):
    share_links = [
        f"https://rwth-aachen.sciebo.de/s/share-{index}" for index in range(4)
    ]
    public_root = "https://rwth-aachen.sciebo.de/public.php/webdav/"
    syncer = make_context({"links.sciebo": True})
    session = FakeSession()
    for link in share_links[:3]:
        session.add(
            "GET",
            link,
            FakeResponse(text=load_fixture("sciebo", "public_share.html")),
        )
    session.add("PROPFIND", public_root, FakeResponse(status_code=503))
    syncer.session = session
    parent = Node("Section", 1, "Section", None)
    caplog.set_level(logging.WARNING, logger="syncmymoodle.sciebo")

    for link in share_links:
        sciebo.scan_public_shares(syncer, link, parent)

    assert session.calls == [
        call
        for link in share_links[:3]
        for call in (("GET", link), ("PROPFIND", public_root))
    ]
    assert caplog.messages == [
        "Sciebo transient failure: WebDAV returned HTTP 503",
        "Sciebo transient failure: WebDAV returned HTTP 503",
        "Sciebo unavailable after 3 consecutive transient failures: WebDAV returned "
        "HTTP 503; skipping remaining requests for this sync. Check the RWTH ITC "
        f"status page: {sciebo.RWTH_SCIEBO_STATUS_URL}",
    ]
    assert parent.children == []
    assert not any(syncer.sciebo_link_cache.values())


def test_sciebo_timeouts_open_shared_service_circuit_without_tracebacks(caplog):
    share_links = [
        f"https://rwth-aachen.sciebo.de/s/share-{index}" for index in range(4)
    ]
    syncer = make_context({"links.sciebo": True})
    session = FakeSession()

    def time_out(url, kwargs):
        raise requests.ReadTimeout("read timed out")

    for link in share_links[:3]:
        session.add("GET", link, time_out)
    syncer.session = session
    parent = Node("Section", 1, "Section", None)
    caplog.set_level(logging.WARNING, logger="syncmymoodle.sciebo")

    for link in share_links:
        sciebo.scan_public_shares(syncer, link, parent)

    assert session.calls == [("GET", link) for link in share_links[:3]]
    assert caplog.messages == [
        "Sciebo transient failure: share page request failed: read timed out",
        "Sciebo transient failure: share page request failed: read timed out",
        "Sciebo unavailable after 3 consecutive transient failures: share page request "
        "failed: read timed out; skipping remaining requests for this sync. Check the "
        f"RWTH ITC status page: {sciebo.RWTH_SCIEBO_STATUS_URL}",
    ]
    assert parent.children == []


def test_youtube_links_use_canonical_video_identity():
    syncer = make_context({"links.youtube": True})
    root = Node("", -1, "Root", None)
    parent = root.add_child("Section", 1, "Section")
    links.scan_for_links(
        syncer,
        "https://youtu.be/abcdefghijk "
        "https://www.youtube.com/watch?v=abcdefghijk&feature=share "
        "https://www.youtube.com/embed/abcdefghijk",
        parent,
        101,
    )

    assert len(parent.children) == 1
    child = parent.children[0]
    assert child.id == "abcdefghijk"
    assert child.url == "https://www.youtube.com/watch?v=abcdefghijk"
    assert child.name == "Youtube: https://www.youtube.com/watch?v=abcdefghijk"
    assert links.youtube_video_id(child.url) == "abcdefghijk"


def test_direct_link_redirect_cannot_bypass_allowed_domains(caplog):
    original_url = "https://files.allowed.test/document"
    external_url = "https://files.example.test/private.pdf"
    syncer = make_context(
        {
            "links.follow_links": True,
            "links.youtube": False,
            "links.opencast": False,
            "links.sciebo": False,
            "filters.allowed_domains": ["files.allowed.test"],
        }
    )
    session = FakeSession()
    session.add(
        "HEAD",
        original_url,
        FakeResponse(status_code=302, headers={"Location": external_url}),
    )
    syncer.session = session
    parent = Node("Section", 1, "Section", None)
    caplog.set_level(logging.WARNING, logger="syncmymoodle.links")

    links.scan_for_links(syncer, original_url, parent, 101, single=True)

    assert session.calls == [("HEAD", original_url)]
    assert parent.children == []
    assert caplog.messages == []
    assert {item.config_key for item in syncer.filtered_items} == {
        "filters.allowed_domains"
    }


def test_generic_link_resource_errors_are_quiet_and_not_scanned(caplog):
    caplog.set_level(logging.WARNING, logger="syncmymoodle.links")

    for status_code in (403, 404):
        url = f"https://files.example.test/error-{status_code}"
        syncer = make_context(
            {
                "links.follow_links": True,
                "links.youtube": True,
                "links.opencast": False,
                "links.sciebo": False,
                "filters.allowed_domains": ["files.example.test"],
            }
        )
        session = FakeSession()
        session.add("HEAD", url, FakeResponse(status_code=status_code))
        session.add(
            "GET",
            url,
            FakeResponse(
                status_code=status_code,
                text="https://youtu.be/abcdefghijk",
            ),
        )
        syncer.session = session
        parent = Node("Section", 1, "Section", None)

        links.scan_for_links(syncer, url, parent, 101, single=True)

        assert session.calls == [("HEAD", url), ("GET", url)]
        assert parent.children == []

    assert caplog.messages == []


def test_generic_link_error_pages_open_origin_circuit_without_being_scanned(caplog):
    urls = [f"https://files.example.test/outage-{index}" for index in range(4)]
    syncer = make_context(
        {
            "links.follow_links": True,
            "links.youtube": True,
            "links.opencast": False,
            "links.sciebo": False,
            "filters.allowed_domains": ["files.example.test"],
        }
    )
    session = FakeSession()
    for url in urls[:3]:
        session.add("HEAD", url, FakeResponse(status_code=503))
        session.add(
            "GET",
            url,
            FakeResponse(status_code=503, text="https://youtu.be/abcdefghijk"),
        )
    syncer.session = session
    parent = Node("Section", 1, "Section", None)
    caplog.set_level(logging.WARNING, logger="syncmymoodle.links")

    for url in urls:
        links.scan_for_links(syncer, url, parent, 101, single=True)

    assert session.calls == [
        call for url in urls[:3] for call in (("HEAD", url), ("GET", url))
    ]
    assert parent.children == []
    assert caplog.messages == [
        "Link origin https://files.example.test transient failure: GET "
        "https://files.example.test/outage-0 returned HTTP 503",
        "Link origin https://files.example.test transient failure: GET "
        "https://files.example.test/outage-1 returned HTTP 503",
        "Link origin https://files.example.test unavailable after 3 consecutive "
        "transient failures: GET https://files.example.test/outage-2 returned HTTP "
        "503; skipping remaining requests for this sync",
    ]


def test_opencast_series_fetches_every_page(monkeypatch):
    syncer = make_context()
    requested_urls = []
    statuses = []

    def fetch_result_list(ctx, url, context, log):
        requested_urls.append(url)
        offset = 100 if "offset=100" in url else 0
        count = 1 if offset else 100
        return [
            {
                "mediapackage": {
                    "id": f"episode-{offset + index}",
                    "title": f"Episode {offset + index}",
                }
            }
            for index in range(count)
        ]

    monkeypatch.setattr(opencast, "fetch_result_list", fetch_result_list)
    monkeypatch.setattr(
        syncer.output.sync_progress,
        "module_status",
        statuses.append,
    )

    episodes = opencast.list_series_episodes(
        syncer,
        "series-123",
        logging.getLogger("test"),
    )

    assert episodes is not None
    assert len(episodes) == 101
    assert requested_urls == [
        f"{opencast.OPENCAST_SEARCH_URL}?limit=100&offset=0&sid=series-123",
        f"{opencast.OPENCAST_SEARCH_URL}?limit=100&offset=100&sid=series-123",
    ]
    assert statuses == [
        "listing Opencast episodes (0 found)",
        "listing Opencast episodes (100 found)",
    ]


def test_opencast_uses_refreshed_episode_from_partial_series(monkeypatch):
    syncer = make_context()
    course_id = 101
    series_id = "series-123"
    episode_id = "episode-0"
    fallback_episode_id = "episode-missing"
    for cached_episode_id in (episode_id, fallback_episode_id):
        opencast.store_episode(
            syncer,
            course_id,
            cached_episode_id,
            opencast.OpencastEpisode(
                (
                    opencast.OpencastTrack(
                        f"https://video.example.test/cached-{cached_episode_id}.mp4"
                    ),
                ),
                series_id,
            ),
            state=None,
        )
    requested_urls = []

    def fetch_result_list(ctx, url, context, log):
        requested_urls.append(url)
        if "?id=" in url:
            return [
                opencast_episode_entry(
                    fallback_episode_id,
                    "https://video.example.test/fallback.mp4",
                    series_id=series_id,
                )
            ]
        if "offset=100" in url:
            return None
        return [
            opencast_episode_entry(
                f"episode-{index}",
                f"https://video.example.test/fresh-{index}.mp4",
                series_id=series_id,
                title=f"Episode {index}",
            )
            for index in range(100)
        ]

    monkeypatch.setattr(opencast, "fetch_result_list", fetch_result_list)
    monkeypatch.setattr(
        opencast,
        "authorize_course_for_episode",
        lambda *args, **kwargs: True,
    )

    series_tracks = opencast.resolve_tracks_from_episode(
        syncer,
        episode_id,
        course_id=course_id,
    )
    fallback_tracks = opencast.resolve_tracks_from_episode(
        syncer,
        fallback_episode_id,
        course_id=course_id,
    )

    assert series_tracks is not None
    assert series_tracks[0].url == "https://video.example.test/fresh-0.mp4"
    assert fallback_tracks is not None
    assert fallback_tracks[0].url == "https://video.example.test/fallback.mp4"
    assert requested_urls == [
        f"{opencast.OPENCAST_SEARCH_URL}?limit=100&offset=0&sid={series_id}",
        f"{opencast.OPENCAST_SEARCH_URL}?limit=100&offset=100&sid={series_id}",
        f"{opencast.OPENCAST_SEARCH_URL}?id={fallback_episode_id}",
    ]


def test_opencast_series_stops_when_backend_repeats_page(monkeypatch, caplog):
    syncer = make_context()
    requested_urls = []
    page = [
        {
            "mediapackage": {
                "id": f"episode-{index}",
                "title": f"Episode {index}",
            }
        }
        for index in range(100)
    ]

    def fetch_result_list(ctx, url, context, log):
        requested_urls.append(url)
        if len(requested_urls) > 2:
            raise AssertionError("pagination did not stop after a repeated page")
        return page

    monkeypatch.setattr(opencast, "fetch_result_list", fetch_result_list)

    episodes = opencast.list_series_episodes(
        syncer,
        "series-123",
        logging.getLogger("test"),
    )

    assert episodes == tuple(
        (f"episode-{index}", f"Episode {index}") for index in range(100)
    )
    assert requested_urls == [
        f"{opencast.OPENCAST_SEARCH_URL}?limit=100&offset=0&sid=series-123",
        f"{opencast.OPENCAST_SEARCH_URL}?limit=100&offset=100&sid=series-123",
    ]
    assert "made no pagination progress at offset 100" in caplog.text


def test_opencast_malformed_series_page_falls_back_to_episode_refresh(monkeypatch):
    syncer = make_context()
    course_id = 101
    series_id = "series-123"
    episode_id = "cached-episode"
    opencast.store_episode(
        syncer,
        course_id,
        episode_id,
        opencast.OpencastEpisode(
            (opencast.OpencastTrack("https://video.example.test/cached.mp4"),),
            series_id,
        ),
        state=None,
    )
    requested_urls = []

    def fetch_result_list(ctx, url, context, log):
        requested_urls.append(url)
        if "sid=" in url:
            return [
                opencast_episode_entry(
                    "other-episode",
                    "https://video.example.test/other.mp4",
                    series_id=series_id,
                    title="other-episode",
                ),
                {"mediapackage": {"title": "Missing id"}},
            ]
        return [
            opencast_episode_entry(
                episode_id,
                "https://video.example.test/refreshed.mp4",
                series_id=series_id,
                title=episode_id,
            )
        ]

    monkeypatch.setattr(opencast, "fetch_result_list", fetch_result_list)
    monkeypatch.setattr(
        opencast,
        "authorize_course_for_episode",
        lambda *args, **kwargs: True,
    )

    tracks = opencast.resolve_tracks_from_episode(
        syncer,
        episode_id,
        course_id=course_id,
    )

    assert tracks is not None
    assert tracks[0].url == "https://video.example.test/refreshed.mp4"
    assert requested_urls == [
        f"{opencast.OPENCAST_SEARCH_URL}?limit=100&offset=0&sid={series_id}",
        f"{opencast.OPENCAST_SEARCH_URL}?id={episode_id}",
    ]


def test_mixed_course_sync_tree_covers_common_module_surfaces(monkeypatch):
    courses = load_json_fixture("moodle", "mixed_courses.json")
    course = load_json_fixture("moodle", "mixed_course.json")
    assignments = load_json_fixture("moodle", "mixed_assignments.json")
    submission_files = load_json_fixture("moodle", "mixed_submission_files.json")
    course[0]["modules"][1]["contents"][0]["filesize"] = 1001
    assignments["assignments"][0]["introattachments"][0]["filesize"] = 1002
    submission_files[0]["filesize"] = 1003
    direct_pdf = "https://files.example.test/direct.pdf"
    html_overview = "https://files.example.test/overview.html"
    page_url = (
        "https://moodle.rwth-aachen.de/pluginfile.php/104/"
        "mod_page/content/315/index.html"
    )
    h5p_package_url = (
        "https://moodle.rwth-aachen.de/pluginfile.php/104/"
        "mod_h5pactivity/package/317/activity.h5p"
    )
    syncer = make_context(
        {
            "modules.assignment": True,
            "modules.resource": True,
            "modules.folder": True,
            "links.youtube": True,
            "links.opencast": True,
            "links.sciebo": False,
        }
    )
    install_moodle_fixtures(
        monkeypatch,
        courses,
        {104: course},
        {104: assignments},
        {412: submission_files},
        {104: load_json_fixture("moodle", "mixed_folders.json")},
    )
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_h5pactivities_by_course",
        lambda session, wstoken, course_id: [
            {
                "id": 317,
                "coursemodule": 317,
                "package": [{"fileurl": h5p_package_url}],
            }
        ],
    )
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "content/content.json", load_fixture("html", "h5p_iframe.html")
        )
    session = FakeSession()
    session.add(
        "HEAD",
        direct_pdf,
        FakeResponse(
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": "1234",
                "ETag": '"direct-file-v1"',
            }
        ),
    )
    session.add(
        "HEAD",
        html_overview,
        FakeResponse(headers={"Content-Type": "text/html"}),
    )
    session.add(
        "GET",
        html_overview,
        FakeResponse(text=load_fixture("html", "external_overview.html")),
    )
    session.add(
        "GET",
        page_url,
        FakeResponse(text=load_fixture("html", "page_module.html")),
    )
    session.add("GET", h5p_package_url, FakeResponse(content=package.getvalue()))
    syncer.session = session
    monkeypatch.setattr(
        opencast,
        "authorize_course_for_episode",
        lambda ctx, course_id, episode_id, *a, **k: True,
    )
    monkeypatch.setattr(
        opencast,
        "resolve_tracks_from_episode",
        lambda ctx, episode_id, *a, **k: (
            opencast.OpencastTrack(
                f"https://video.example.test/{episode_id}/presentation.mp4",
                checksum_type="md5",
                checksum="33333333333333333333333333333333",
                size=5678,
                flavor_type="presentation",
            ),
        ),
    )

    sync.sync(syncer)

    assert session.count("HEAD", direct_pdf) == 1
    assert session.count("GET", direct_pdf) == 0
    assert session.count("HEAD", html_overview) == 1
    assert session.count("GET", html_overview) == 1
    assert session.count("GET", page_url) == 1
    assert session.count("GET", h5p_package_url) == 1
    direct_node = node_at_path(
        syncer.root_node, ["26ss", "Comprehensive Sync", "Materials", "direct.pdf"]
    )
    opencast_node = node_at_path(
        syncer.root_node,
        ["26ss", "Comprehensive Sync", "Materials", "Page module (presentation)"],
    )
    assert direct_node.remote_size == 1234
    assert opencast_node.remote_size == 5678
    base_path = ["26ss", "Comprehensive Sync", "Materials"]
    expected_sizes = {
        ("Data folder", "Data", "Raw", "measurements.csv"): 1001,
        ("Essay upload", "essay-template.docx"): 1002,
        ("Essay upload", "Feedback", "feedback.txt"): 1003,
    }
    for suffix, expected_size in expected_sizes.items():
        node = node_at_path(syncer.root_node, [*base_path, *suffix])
        assert node.remote_size == expected_size
    assert_snapshot("mixed_course_tree.txt", node_rows(syncer.root_node))


def test_opencast_lti_single_and_series_use_lti_and_api_routes(monkeypatch):
    courses = [load_json_fixture("moodle", "courses.json")[0]]
    lti_submit_url = "https://engage.streaming.rwth-aachen.de/lti"
    single_episode = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    series_id = "series-1111-2222"
    series_url = (
        "https://engage.streaming.rwth-aachen.de/search/episode.json"
        f"?limit=100&offset=0&sid={series_id}"
    )
    syncer = make_context(
        {
            "modules.assignment": False,
            "modules.resource": False,
            "modules.folder": False,
            "links.youtube": False,
            "links.opencast": True,
            "links.sciebo": False,
        }
    )
    install_moodle_fixtures(
        monkeypatch,
        courses,
        {101: load_json_fixture("moodle", "opencast_lti_course.json")},
    )
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_ltis_by_course",
        lambda session, wstoken, course_id: [
            {"id": 9001, "coursemodule": 501},
            {"id": 9002, "coursemodule": 502},
        ],
    )
    launch_calls = []

    def launch_data(session, wstoken, tool_id):
        launch_calls.append(tool_id)
        custom_name, custom_value, title = (
            ("custom_id", single_episode, "Single recording")
            if tool_id == 9001
            else ("custom_series", series_id, "Series recordings")
        )
        return {
            "endpoint": lti_submit_url,
            "parameters": [
                {"name": custom_name, "value": custom_value},
                {"name": "resource_link_title", "value": title},
                {"name": "oauth_consumer_key", "value": "fake-consumer"},
                {"name": "oauth_signature", "value": "fake-signature"},
            ],
        }

    monkeypatch.setattr("syncmymoodle.moodle.get_lti_launch_data", launch_data)
    series_payload = load_json_fixture("opencast", "series.json")
    for episode, fixture_name in zip(
        series_payload["result"],
        ("episode_series_a.json", "episode_series_b.json"),
        strict=True,
    ):
        episode["mediapackage"]["media"] = load_json_fixture(
            "opencast",
            fixture_name,
        )["result"][0]["mediapackage"]["media"]
    session = FakeSession()
    session.add("POST", lti_submit_url, FakeResponse(text="ok"))
    session.add(
        "GET",
        "https://engage.streaming.rwth-aachen.de/search/episode.json"
        f"?id={single_episode}",
        FakeResponse(json_payload=load_json_fixture("opencast", "episode_single.json")),
    )
    session.add(
        "GET",
        series_url,
        FakeResponse(json_payload=series_payload),
    )
    syncer.session = session

    sync.sync(syncer)

    assert launch_calls == [9001, 9002]
    assert session.count("POST", lti_submit_url) == 1
    assert session.count("GET") == 2
    assert_snapshot("opencast_lti_tree.txt", node_rows(syncer.root_node))
