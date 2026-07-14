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


def test_cached_module_data_survives_course_name_disambiguation(tmp_path):
    def colliding_courses(context):
        root = Node("", -1, "Root", None)
        semester = root.add_child("26ss", None, "Semester")
        first = semester.add_child("Same Course", 301, "Course")
        second = semester.add_child("Same Course", 302, "Course")
        context.root_node = root
        return root, first, second

    context = make_context({"paths.sync_directory": str(tmp_path)})
    root, first, second = colliding_courses(context)
    course_cache.store_cached_text(
        context,
        first,
        course_cache.H5P_CONTENT_KIND,
        11,
        "marker-1",
        "first",
    )
    course_cache.store_cached_text(
        context,
        second,
        course_cache.H5P_CONTENT_KIND,
        22,
        "marker-2",
        "second",
    )

    root.remove_children_nameclashes()
    course_cache.cache_root_node(context)

    loaded = make_context({"paths.sync_directory": str(tmp_path)})
    loaded_root, loaded_first, loaded_second = colliding_courses(loaded)
    loaded_root.remove_children_nameclashes()

    assert (
        course_cache.get_cached_text(
            loaded,
            loaded_first,
            course_cache.H5P_CONTENT_KIND,
            11,
            "marker-1",
        ).content
        == "first"
    )
    assert (
        course_cache.get_cached_text(
            loaded,
            loaded_second,
            course_cache.H5P_CONTENT_KIND,
            22,
            "marker-2",
        ).content
        == "second"
    )


def test_sync_prunes_cache_entries_for_removed_modules(tmp_path, monkeypatch):
    config = {"paths.sync_directory": str(tmp_path)}
    seeded = make_context(config)
    seeded.moodle_server_time = 100
    seeded.root_node, course_node = course_tree()
    course_cache.store_cached_text(
        seeded,
        course_node,
        course_cache.H5P_CONTENT_KIND,
        11,
        "h5p-marker",
        "h5p content",
    )
    course_cache.store_cached_text(
        seeded,
        course_node,
        course_cache.PAGE_CONTENT_KIND,
        12,
        "page-marker",
        "page content",
        "https://example.test/page",
    )
    course_cache.store_assignment_cache_entry(seeded, course_node, 13, [])
    course_cache.store_quiz_cache_entry(
        seeded,
        course_node,
        14,
        [{"id": 15, "timefinish": 1}],
        {15: {"questions": []}},
        0,
        None,
    )
    course_cache.cache_root_node(seeded)

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
        lambda session, wstoken, course_id: [
            {"id": 401, "name": "General", "modules": []}
        ],
    )
    current = make_context(config)
    current.session = FakeSession()

    sync.sync(current)
    course_cache.cache_root_node(current)

    loaded = make_context(config)
    _, loaded_course = course_tree()
    assert (
        course_cache.get_cached_text(
            loaded,
            loaded_course,
            course_cache.H5P_CONTENT_KIND,
            11,
            "h5p-marker",
        )
        is None
    )
    assert (
        course_cache.get_cached_text(
            loaded,
            loaded_course,
            course_cache.PAGE_CONTENT_KIND,
            12,
            "page-marker",
        )
        is None
    )
    assert course_cache.get_assignment_cache_entry(loaded, loaded_course, 13) is None
    assert course_cache.get_quiz_cache_entry(loaded, loaded_course, 14) is None


def test_malformed_module_inventory_does_not_prune_cache(tmp_path, monkeypatch):
    config = {"paths.sync_directory": str(tmp_path)}
    seeded = make_context(config)
    seeded.root_node, course_node = course_tree()
    course_cache.store_cached_text(
        seeded,
        course_node,
        course_cache.H5P_CONTENT_KIND,
        11,
        "h5p-marker",
        "h5p content",
    )
    course_cache.cache_root_node(seeded)

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
        lambda session, wstoken, course_id: [
            {"id": 401, "name": "General", "modules": [{"name": "broken"}]}
        ],
    )
    current = make_context(config)
    current.session = FakeSession()

    sync.sync(current)
    course_cache.cache_root_node(current)

    loaded = make_context(config)
    _, loaded_course = course_tree()
    entry = course_cache.get_cached_text(
        loaded,
        loaded_course,
        course_cache.H5P_CONTENT_KIND,
        11,
        "h5p-marker",
    )
    assert entry is not None
    assert entry.content == "h5p content"
