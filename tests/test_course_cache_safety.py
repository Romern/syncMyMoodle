from syncmymoodle import course_cache, moodle, sync
from syncmymoodle.constants import COURSE_CACHE_FILENAME
from syncmymoodle.node import Node
from syncmymoodle.storage import write_private_gzip_json

from .helpers import FakeSession, make_context, node_path


def course_tree():
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    course = semester.add_child("Download Course", 301, "Course")
    section = course.add_child("General", 401, "Section")
    file_node = section.add_child(
        "slides.pdf",
        "file-id",
        "Linked file [application/pdf]",
        url="https://example.test/slides.pdf",
        timemodified=100,
        etag='"v1"',
    )
    file_node.mark_handled()
    return root, course


def test_failed_course_fetch_preserves_previous_course_cache(tmp_path, monkeypatch):
    config = {"paths.sync_directory": str(tmp_path)}
    cached_context = make_context(config)
    cached_context.root_node, course_node = course_tree()
    course_cache.cache_root_node(cached_context)
    cache_path = node_path(cached_context, course_node) / COURSE_CACHE_FILENAME
    cached_bytes = cache_path.read_bytes()
    context = make_context(config)
    context.session = FakeSession()
    monkeypatch.setattr(
        moodle,
        "get_all_courses",
        lambda session, wstoken, user_id: [
            {"id": 301, "shortname": "Download Course", "idnumber": "26ss"}
        ],
    )
    monkeypatch.setattr(
        moodle,
        "get_course",
        lambda session, wstoken, course_id: None,
    )

    sync.sync(context)
    course_cache.cache_root_node(context)

    assert context.root_node is not None
    assert context.root_node.children == []
    assert cache_path.read_bytes() == cached_bytes


def test_malformed_nested_course_cache_is_ignored(tmp_path, caplog):
    context = make_context({"paths.sync_directory": str(tmp_path)})
    _, course_node = course_tree()
    cache_path = node_path(context, course_node) / COURSE_CACHE_FILENAME
    write_private_gzip_json(
        cache_path,
        {
            "format": course_cache.COURSE_CACHE_FORMAT,
            "course": {
                "name": course_node.name,
                "id": course_node.id,
                "type": "Course",
                "children": 1,
            },
        },
    )

    assert course_cache.get_course_cache_root(context, course_node) is None
    assert "Ignoring malformed course cache" in caplog.text
