import pytest

from syncmymoodle import course_cache, moodle, opencast, pathing, sync, sync_handlers
from syncmymoodle.constants import COURSE_CACHE_FILENAME, MOODLE_URL
from syncmymoodle.context import MoodleAccount
from syncmymoodle.moodle_tokens import MoodleTokens
from syncmymoodle.node import DownloadKind, Node
from syncmymoodle.storage import read_private_gzip_json, write_private_gzip_json

from .helpers import FakeSession, make_context, node_path


def symlink_directory(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are not available: {error}")


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


def tagged_v1_payload(*, site=MOODLE_URL):
    file_url = f"{site.rstrip('/')}/pluginfile.php/301/slides.pdf"
    return {
        "format": course_cache.LEGACY_COURSE_CACHE_FORMAT,
        "course": {
            "name": "Download Course",
            "id": 301,
            "type": "Course",
            "download_status": "pending",
            "children": [
                {
                    "name": "General",
                    "id": 401,
                    "type": "Section",
                    "download_status": "pending",
                    "children": [
                        {
                            "name": "slides.pdf",
                            "id": "file-id",
                            "type": "Linked file [application/pdf]",
                            "url": file_url,
                            "timemodified": 100,
                            "download_status": "handled",
                            "children": [],
                        }
                    ],
                }
            ],
        },
    }


def test_failed_course_fetch_preserves_previous_course_cache(tmp_path, monkeypatch):
    config = {"paths.sync_directory": str(tmp_path)}
    cached_context = make_context(config)
    cached_context.root_node, course_node = course_tree()
    course_cache.cache_root_node(cached_context)
    cache_path = course_cache.course_cache_path(cached_context, course_node)
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
    config = {"paths.sync_directory": str(tmp_path)}
    writer = make_context(config)
    writer.root_node, course_node = course_tree()
    course_cache.cache_root_node(writer)
    cache_path = course_cache.course_cache_path(writer, course_node)
    payload = read_private_gzip_json(cache_path, "course cache")
    assert isinstance(payload, dict)
    payload["course"]["children"] = 1
    write_private_gzip_json(cache_path, payload)

    reader = make_context(config)
    _, reader_course = course_tree()
    assert course_cache.get_course_cache_root(reader, reader_course) is None
    assert "Ignoring malformed course cache" in caplog.text


def test_course_cache_survives_course_rename(tmp_path):
    config = {"paths.sync_directory": str(tmp_path)}
    seeded = make_context(config)
    seeded.root_node, course_node = course_tree()
    course_cache.store_cached_text(
        seeded,
        course_node,
        course_cache.H5P_CONTENT_KIND,
        11,
        "marker",
        "cached page",
    )
    course_cache.cache_root_node(seeded)

    loaded = make_context(config)
    _, renamed_course = course_tree()
    renamed_course.name = "Renamed Course"

    entry = course_cache.get_cached_text(
        loaded,
        renamed_course,
        course_cache.H5P_CONTENT_KIND,
        11,
        "marker",
    )
    assert entry is not None
    assert entry.content == "cached page"


def test_tagged_v1_course_cache_is_moved_from_renamed_course_directory(tmp_path):
    config = {"paths.sync_directory": str(tmp_path)}
    context = make_context(config)
    _, old_course = course_tree()
    legacy_path = node_path(context, old_course) / COURSE_CACHE_FILENAME
    write_private_gzip_json(legacy_path, tagged_v1_payload())

    _, renamed_course = course_tree()
    renamed_course.name = "Renamed Course"
    cached_root = course_cache.get_course_cache_root(context, renamed_course)

    assert cached_root is not None
    cached_file = cached_root.children[0].children[0]
    assert cached_file.is_verified
    assert cached_file.download_kind is DownloadKind.DIRECT
    cache_path = course_cache.course_cache_path(context, renamed_course)
    migrated = read_private_gzip_json(cache_path, "course cache")
    assert isinstance(migrated, dict)
    assert migrated["format"] == course_cache.COURSE_CACHE_FORMAT
    assert migrated["identity"] == {
        "site": MOODLE_URL,
        "user_id": 10001,
        "course_id": 301,
    }
    assert not legacy_path.exists()


def test_legacy_course_cache_from_another_site_is_not_moved(tmp_path):
    config = {"paths.sync_directory": str(tmp_path)}
    context = make_context(config)
    _, course = course_tree()
    legacy_path = node_path(context, course) / COURSE_CACHE_FILENAME
    write_private_gzip_json(
        legacy_path,
        tagged_v1_payload(site="https://other-moodle.example/"),
    )

    assert course_cache.get_course_cache_root(context, course) is None
    assert legacy_path.exists()
    assert not course_cache.course_cache_path(context, course).exists()


def test_legacy_cache_drops_personal_nodes(tmp_path):
    config = {"paths.sync_directory": str(tmp_path)}
    context = make_context(config)
    _, course = course_tree()
    payload = tagged_v1_payload()
    section_children = payload["course"]["children"][0]["children"]
    section_children.extend(
        [
            {
                "name": "my-submission.pdf",
                "id": "submission-id",
                "type": "Assignment File",
                "url": f"{MOODLE_URL}pluginfile.php/301/submission.pdf",
                "download_status": "handled",
                "children": [],
            },
            {
                "name": "Attempt 1",
                "id": 123,
                "type": "Quiz",
                "url": f"{MOODLE_URL}mod/quiz/review.php?attempt=123",
                "download_status": "handled",
                "children": [],
            },
        ]
    )
    legacy_path = node_path(context, course) / COURSE_CACHE_FILENAME
    write_private_gzip_json(legacy_path, payload)

    cached_root = course_cache.get_course_cache_root(context, course)

    assert cached_root is not None
    assert [child.name for child in cached_root.children[0].children] == ["slides.pdf"]
    migrated = read_private_gzip_json(
        course_cache.course_cache_path(context, course), "course cache"
    )
    assert isinstance(migrated, dict)
    assert course_cache.MODULE_CACHE_KEY not in migrated


def test_unshipped_legacy_cache_extensions_are_not_migrated(tmp_path):
    context = make_context({"paths.sync_directory": str(tmp_path)})
    _, course = course_tree()
    payload = tagged_v1_payload()
    payload[course_cache.MODULE_CACHE_KEY] = {}
    legacy_path = node_path(context, course) / COURSE_CACHE_FILENAME
    write_private_gzip_json(legacy_path, payload)

    assert course_cache.get_course_cache_root(context, course) is None
    assert legacy_path.exists()
    assert not course_cache.course_cache_path(context, course).exists()


def test_legacy_cache_tree_is_scanned_once_per_run(tmp_path, monkeypatch):
    context = make_context({"paths.sync_directory": str(tmp_path)})
    _, old_course = course_tree()
    legacy_path = node_path(context, old_course) / COURSE_CACHE_FILENAME
    write_private_gzip_json(legacy_path, tagged_v1_payload())

    rglob = type(tmp_path).rglob
    calls = 0

    def counting_rglob(path, pattern):
        nonlocal calls
        calls += 1
        return rglob(path, pattern)

    monkeypatch.setattr(type(tmp_path), "rglob", counting_rglob)
    _, renamed_course = course_tree()
    renamed_course.name = "Renamed Course"
    assert course_cache.get_course_cache_root(context, renamed_course) is not None

    semester = renamed_course.parent
    assert semester is not None
    other_course = semester.add_child("Other Course", 302, "Course")
    assert course_cache.get_course_cache_root(context, other_course) is None
    assert calls == 1


def test_course_cache_path_applies_long_windows_path_support(tmp_path, monkeypatch):
    context = make_context({"paths.sync_directory": str(tmp_path)})
    _, course = course_tree()
    monkeypatch.setattr(pathing, "is_windows", lambda: True)
    monkeypatch.setattr(pathing, "WINDOWS_EXTENDED_PATH_THRESHOLD", 0)

    cache_path = course_cache.course_cache_path(context, course)

    assert str(cache_path).startswith("\\\\?\\")


def test_course_cache_refuses_a_linked_internal_parent(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    symlink_directory(root / course_cache.COURSE_CACHE_DIRECTORY, outside)
    context = make_context({"paths.sync_directory": str(root)})
    context.root_node, course = course_tree()

    with pytest.raises(
        pathing.UnsafeInternalPathError, match="Refusing linked internal path"
    ):
        course_cache.cache_root_node(context)

    assert list(outside.iterdir()) == []


def test_course_cache_resolves_a_linked_configured_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    configured_root = tmp_path / "configured-root"
    symlink_directory(configured_root, target)
    context = make_context({"paths.sync_directory": str(configured_root)})
    _, course = course_tree()

    cache_path = course_cache.course_cache_path(context, course)

    assert cache_path.is_relative_to(target)


def test_course_cache_keeps_the_root_resolved_for_the_context(tmp_path):
    first_target = tmp_path / "first-target"
    second_target = tmp_path / "second-target"
    first_target.mkdir()
    second_target.mkdir()
    configured_root = tmp_path / "configured-root"
    symlink_directory(configured_root, first_target)
    context = make_context({"paths.sync_directory": str(configured_root)})
    configured_root.unlink()
    symlink_directory(configured_root, second_target)
    _, course = course_tree()

    cache_path = course_cache.course_cache_path(context, course)

    assert cache_path.is_relative_to(first_target)
    assert not cache_path.is_relative_to(second_target)


def test_course_cache_is_isolated_by_moodle_account(tmp_path):
    config = {"paths.sync_directory": str(tmp_path)}
    seeded = make_context(config)
    seeded.root_node, course_node = course_tree()
    course_cache.cache_root_node(seeded)

    other = make_context(config)
    other.moodle_account = MoodleAccount(
        MoodleTokens(
            "other-user",
            "other-token",
            "other-private-token",
            moodle_user_id=10002,
        )
    )
    _, other_course = course_tree()

    assert course_cache.course_cache_path(
        seeded, course_node
    ) != course_cache.course_cache_path(other, other_course)
    assert course_cache.get_course_cache_root(other, other_course) is None


def test_module_handler_failure_preserves_previous_course_cache(
    tmp_path,
    monkeypatch,
):
    config = {"paths.sync_directory": str(tmp_path)}
    seeded = make_context(config)
    seeded.root_node, course_node = course_tree()
    course_cache.cache_root_node(seeded)
    cache_path = course_cache.course_cache_path(seeded, course_node)
    cached_bytes = cache_path.read_bytes()

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
            {
                "id": 401,
                "name": "General",
                "modules": [
                    {"id": 501, "modname": "resource", "name": "Broken module"}
                ],
            }
        ],
    )

    def fail_module(module_context, module):
        raise RuntimeError("boom")

    monkeypatch.setattr(sync_handlers, "handle_module", fail_module)
    current = make_context(config)
    current.session = FakeSession()

    sync.sync(current)
    course_cache.cache_root_node(current)

    assert current.stats.failed == 1
    assert cache_path.read_bytes() == cached_bytes


@pytest.mark.parametrize(
    ("module_kind", "config_key", "inventory_function"),
    [
        ("assign", "modules.assignment", "get_assignment"),
        ("folder", "modules.folder", "get_folders_by_courses"),
    ],
)
def test_auxiliary_inventory_outage_fails_run_without_stopping_modules_or_cache(
    tmp_path,
    monkeypatch,
    module_kind,
    config_key,
    inventory_function,
):
    config = {"paths.sync_directory": str(tmp_path), config_key: True}
    seeded = make_context(config)
    seeded.root_node, course_node = course_tree()
    course_cache.cache_root_node(seeded)
    cache_path = course_cache.course_cache_path(seeded, course_node)
    cached_bytes = cache_path.read_bytes()

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
            {
                "id": 401,
                "name": "General",
                "modules": [
                    {"id": 501, "modname": module_kind, "name": "Unavailable"},
                    {"id": 502, "modname": "label", "name": "Still handled"},
                ],
            }
        ],
    )
    monkeypatch.setattr(moodle, inventory_function, lambda *args: None)
    handled = []
    monkeypatch.setattr(
        sync_handlers,
        "handle_module",
        lambda module_context, module: handled.append(module["id"]),
    )
    current = make_context(config)
    current.session = FakeSession()

    sync.sync(current)
    course_cache.cache_root_node(current)

    assert handled == [501, 502]
    assert current.stats.failed == 1
    assert current.incomplete_course_ids == {301}
    assert cache_path.read_bytes() == cached_bytes


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

    pathing.resolve_node_path_clashes(root)
    course_cache.cache_root_node(context)

    loaded = make_context({"paths.sync_directory": str(tmp_path)})
    loaded_root, loaded_first, loaded_second = colliding_courses(loaded)
    pathing.resolve_node_path_clashes(loaded_root)

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


def test_persisted_opencast_series_refreshes_in_one_request(tmp_path, monkeypatch):
    config = {"paths.sync_directory": str(tmp_path)}
    seeded = make_context(config)
    seeded.root_node, course_node = course_tree()
    series_id = "series-1111-2222"
    episode_ids = (
        "11111111-2222-4333-8444-555555555555",
        "66666666-7777-4888-8999-000000000000",
    )
    for episode_id in episode_ids:
        opencast.store_episode(
            seeded,
            course_node.id,
            episode_id,
            opencast.OpencastEpisode(
                (
                    opencast.OpencastTrack(
                        f"https://video.example.test/{episode_id}.mp4"
                    ),
                ),
                series_id,
            ),
        )
    course_cache.cache_root_node(seeded)

    loaded = make_context(config)
    _, loaded_course = course_tree()
    course_cache.get_course_cache_root(loaded, loaded_course)
    requested_urls = []

    monkeypatch.setattr(
        opencast,
        "authorize_course_for_episode",
        lambda *args, **kwargs: True,
    )

    def fetch_result_list(ctx, url, context, log):
        requested_urls.append(url)
        return [
            {
                "mediapackage": {
                    "id": episode_id,
                    "series": series_id,
                    "title": episode_id,
                    "media": {
                        "track": {
                            "type": "presentation/delivery",
                            "mimetype": "video/mp4",
                            "url": f"https://video.example.test/fresh-{episode_id}.mp4",
                            "video": {"resolution": "1920x1080"},
                        }
                    },
                }
            }
            for episode_id in episode_ids
        ]

    monkeypatch.setattr(opencast, "fetch_result_list", fetch_result_list)

    tracks = [
        opencast.resolve_tracks_from_episode(
            loaded,
            episode_id,
            course_id=loaded_course.id,
        )
        for episode_id in episode_ids
    ]

    assert requested_urls == [
        f"{opencast.OPENCAST_SEARCH_URL}?limit=100&offset=0&sid={series_id}"
    ]
    assert [resolved[0].url for resolved in tracks if resolved is not None] == [
        f"https://video.example.test/fresh-{episode_id}.mp4"
        for episode_id in episode_ids
    ]


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


def test_malformed_sections_do_not_stop_later_courses(tmp_path, monkeypatch):
    monkeypatch.setattr(
        moodle,
        "get_all_courses",
        lambda session, wstoken, user_id: [
            {"id": 301, "shortname": "Malformed", "idnumber": "26ss"},
            {"id": 302, "shortname": "Healthy", "idnumber": "26ss"},
        ],
    )

    def course_contents(session, wstoken, course_id):
        if course_id == 301:
            return [{"id": 401, "name": "General", "modules": None}]
        return [{"id": 402, "name": "General", "modules": []}]

    monkeypatch.setattr(moodle, "get_course", course_contents)
    context = make_context({"paths.sync_directory": str(tmp_path)})
    context.session = FakeSession()

    sync.sync(context)

    assert context.stats.failed == 1
    assert context.incomplete_course_ids == {301}
    assert context.root_node is not None
    courses = {
        course.id: course
        for semester in context.root_node.children
        for course in semester.children
    }
    assert set(courses) == {301, 302}
    assert [section.name for section in courses[302].children] == ["General"]
