from pathlib import Path
from typing import Any

import pytest

from syncmymoodle import course_cache, downloader, moodle, sync, sync_handlers
from syncmymoodle.context import SyncContext
from syncmymoodle.node import DownloadKind, Node
from syncmymoodle.outcomes import RemovedContent
from syncmymoodle.storage import read_private_gzip_json

from .helpers import FakeSession, make_context, node_path

COURSE_ID = 301
MODULE_ID = 501
RESOURCE_URL = "https://files.example.test/notes.pdf?token=private-token&item=456"
EXPECTED_REMOVAL = RemovedContent(
    "Download Course (301)",
    "General/notes.pdf",
    "https://files.example.test/notes.pdf?token=[REDACTED]&item=456",
)
MISSING = object()


def resource_module() -> dict[str, Any]:
    return {
        "id": MODULE_ID,
        "modname": "resource",
        "name": "Lecture notes",
        "contents": [
            {
                "type": "file",
                "filename": "notes.pdf",
                "fileurl": RESOURCE_URL,
                "mimetype": "application/pdf",
            }
        ],
    }


def folder_module(contents: object) -> dict[str, Any]:
    return {
        "id": MODULE_ID,
        "modname": "folder",
        "name": "Materials",
        "contents": contents,
    }


def assignment_module() -> dict[str, Any]:
    return {
        "id": MODULE_ID,
        "instance": 601,
        "modname": "assign",
        "name": "Homework",
    }


def module_context(
    config: dict[str, Any] | None = None,
) -> tuple[SyncContext, sync_handlers.ModuleContext]:
    context = make_context(config)
    context.session = FakeSession()
    course = Node("Download Course", COURSE_ID, "Course", None)
    section = course.add_child("General", 401, "Section")
    return context, sync_handlers.ModuleContext(
        context,
        COURSE_ID,
        course,
        section,
        {},
        {},
    )


def install_course_inventory(
    monkeypatch,
    modules: list[dict[str, Any]],
    *,
    section_name: str = "General",
) -> None:
    monkeypatch.setattr(
        moodle,
        "get_all_courses",
        lambda *args: [
            {
                "id": COURSE_ID,
                "shortname": "Download Course",
                "idnumber": "26ss",
            }
        ],
    )
    monkeypatch.setattr(
        moodle,
        "get_course",
        lambda *args: [{"id": 401, "name": section_name, "modules": modules}],
    )


def seed_resource_cache(tmp_path, monkeypatch) -> tuple[dict[str, Any], Path]:
    config = {"paths.sync_directory": str(tmp_path)}
    install_course_inventory(monkeypatch, [resource_module()])
    context = make_context(config)
    context.session = FakeSession()
    sync.sync(context)
    assert context.root_node is not None
    resource = context.root_node.children[0].children[0].children[0].children[0]
    local_file = node_path(context, resource)
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"local copy")
    course_cache.cache_root_node(context)
    return config, local_file


def cached_course_payload(config: dict[str, Any]) -> dict[str, Any]:
    lookup = make_context(config)
    course = Node("Download Course", COURSE_ID, "Course", None)
    payload = read_private_gzip_json(
        course_cache.course_cache_path(lookup, course),
        "course cache",
    )
    assert isinstance(payload, dict)
    return payload


def test_authoritative_removal_is_reported_without_deleting_local_copy(
    tmp_path,
    monkeypatch,
):
    config, local_file = seed_resource_cache(tmp_path, monkeypatch)
    install_course_inventory(monkeypatch, [])

    current = make_context(config)
    current.session = FakeSession()
    sync.sync(current)

    assert current.removed_content == {EXPECTED_REMOVAL}
    assert local_file.read_bytes() == b"local copy"


def test_complete_inventory_persists_scope_fingerprint(tmp_path, monkeypatch):
    config, _ = seed_resource_cache(tmp_path, monkeypatch)
    payload = cached_course_payload(config)

    scope = payload[course_cache.INVENTORY_SCOPE_CACHE_KEY]
    assert isinstance(scope, str)
    assert len(scope) == 64
    assert all(character in "0123456789abcdef" for character in scope)


def test_same_remote_identity_at_a_new_name_is_not_reported_removed(
    tmp_path,
    monkeypatch,
):
    config, _ = seed_resource_cache(tmp_path, monkeypatch)
    install_course_inventory(
        monkeypatch,
        [resource_module()],
        section_name="Renamed section",
    )

    current = make_context(config)
    current.session = FakeSession()
    sync.sync(current)

    assert current.removed_content == set()


def test_failed_module_scan_does_not_report_cached_content_removed(
    tmp_path,
    monkeypatch,
):
    config, _ = seed_resource_cache(tmp_path, monkeypatch)
    install_course_inventory(monkeypatch, [resource_module()])

    def fail_module(*args):
        raise RuntimeError("temporary provider failure")

    monkeypatch.setattr(sync_handlers, "handle_module", fail_module)
    current = make_context(config)
    current.session = FakeSession()
    sync.sync(current)

    assert current.stats.failed == 1
    assert current.incomplete_course_ids == {COURSE_ID}
    assert current.removed_content == set()

    course_cache.cache_root_node(current)
    install_course_inventory(monkeypatch, [])
    recovered = make_context(config)
    recovered.session = FakeSession()
    sync.sync(recovered)

    assert recovered.removed_content == {EXPECTED_REMOVAL}


def test_malformed_course_inventory_does_not_report_cached_content_removed(
    tmp_path,
    monkeypatch,
):
    config, _ = seed_resource_cache(tmp_path, monkeypatch)
    install_course_inventory(monkeypatch, [])
    monkeypatch.setattr(
        moodle,
        "get_course",
        lambda *args: [{"id": 401, "name": "General", "modules": None}],
    )

    current = make_context(config)
    current.session = FakeSession()
    sync.sync(current)

    assert current.stats.failed == 1
    assert current.incomplete_course_ids == {COURSE_ID}
    assert current.removed_content == set()


@pytest.mark.parametrize(
    "contents",
    [MISSING, None, [], ["malformed-content"]],
    ids=["missing", "null", "empty", "malformed-entry"],
)
def test_non_authoritative_resource_contents_suppress_removal(
    tmp_path,
    monkeypatch,
    contents,
):
    config, _ = seed_resource_cache(tmp_path, monkeypatch)
    module = resource_module()
    if contents is MISSING:
        del module["contents"]
    else:
        module["contents"] = contents
    install_course_inventory(monkeypatch, [module])

    current = make_context(config)
    current.session = FakeSession()
    sync.sync(current)

    assert current.stats.failed == 1
    assert current.incomplete_course_ids == {COURSE_ID}
    assert current.removed_content == set()


@pytest.mark.parametrize("contents", [MISSING, []], ids=["missing", "empty"])
def test_unavailable_resource_without_contents_is_not_a_failure(
    tmp_path,
    monkeypatch,
    contents,
    caplog,
):
    config, _ = seed_resource_cache(tmp_path, monkeypatch)
    cached_payload = cached_course_payload(config)
    module = resource_module()
    module["uservisible"] = False
    module["availabilityinfo"] = "Available later"
    if contents is MISSING:
        del module["contents"]
    else:
        module["contents"] = contents
    install_course_inventory(monkeypatch, [module])

    current = make_context(config)
    current.session = FakeSession()
    sync.sync(current)
    course_cache.cache_root_node(current)

    assert current.stats.failed == 0
    assert current.incomplete_course_ids == set()
    assert current.inventory_filtered_course_ids == {COURSE_ID}
    assert current.removed_content == set()
    assert cached_course_payload(config) != cached_payload
    assert "malformed resource content inventory" not in caplog.text


@pytest.mark.parametrize(
    ("contents", "expected_failures"),
    [(MISSING, 0), (["malformed-content"], 1)],
    ids=["missing-is-valid", "malformed-entry"],
)
def test_url_module_contents_validate_only_entries(
    monkeypatch,
    contents,
    expected_failures,
):
    module = {"id": MODULE_ID, "modname": "url", "name": "External link"}
    if contents is not MISSING:
        module["contents"] = contents
    install_course_inventory(monkeypatch, [module])
    current = make_context()
    current.session = FakeSession()

    sync.sync(current)

    assert current.stats.failed == expected_failures
    assert current.incomplete_course_ids == (
        {COURSE_ID} if expected_failures else set()
    )


def test_assignment_inventory_cross_source_mismatch_suppresses_removal(
    tmp_path,
    monkeypatch,
):
    config = {"paths.sync_directory": str(tmp_path)}
    install_course_inventory(monkeypatch, [assignment_module()])
    monkeypatch.setattr(
        moodle,
        "get_assignment",
        lambda *args: {
            "assignments": [
                {
                    "id": 601,
                    "cmid": MODULE_ID,
                    "introattachments": resource_module()["contents"],
                }
            ]
        },
    )
    monkeypatch.setattr(
        moodle,
        "get_assignment_submission_files",
        lambda *args: [],
    )
    baseline = make_context(config)
    baseline.session = FakeSession()
    sync.sync(baseline)
    course_cache.cache_root_node(baseline)

    monkeypatch.setattr(
        moodle,
        "get_assignment",
        lambda *args: {"assignments": []},
    )
    current = make_context(config)
    current.session = FakeSession()
    sync.sync(current)

    assert current.stats.failed == 1
    assert current.incomplete_course_ids == {COURSE_ID}
    assert current.removed_content == set()


@pytest.mark.parametrize("activity", [None, {"coursemodule": MODULE_ID, "package": []}])
def test_h5p_missing_activity_or_package_fails_once(monkeypatch, activity):
    context, handler_context = module_context()
    monkeypatch.setattr(
        moodle,
        "get_h5pactivities_by_course",
        lambda *args: [] if activity is None else [activity],
    )

    sync_handlers.handle_embedded_link_module(
        handler_context,
        {"id": MODULE_ID, "modname": "h5pactivity", "name": "Interactive"},
    )

    assert context.stats.failed == 1
    assert context.incomplete_course_ids == {COURSE_ID}


def test_lti_missing_api_and_core_instance_fails_once(monkeypatch):
    context, handler_context = module_context({"links.opencast": True})
    monkeypatch.setattr(moodle, "get_ltis_by_course", lambda *args: [])

    launch = sync_handlers._opencast_lti_launch(
        handler_context,
        {"id": MODULE_ID, "modname": "lti", "name": "Recording"},
    )

    assert launch is None
    assert context.stats.failed == 1
    assert context.incomplete_course_ids == {COURSE_ID}


def test_quiz_missing_api_and_core_instance_fails_once(monkeypatch):
    context, handler_context = module_context({"modules.quiz": "html"})
    monkeypatch.setattr(moodle, "get_quizzes_by_course", lambda *args: [])

    sync_handlers.handle_quiz_module(
        handler_context,
        {"id": MODULE_ID, "modname": "quiz", "name": "Exam"},
    )

    assert context.stats.failed == 1
    assert context.incomplete_course_ids == {COURSE_ID}


def test_new_module_filter_does_not_report_cached_content_removed(
    tmp_path,
    monkeypatch,
):
    config, _ = seed_resource_cache(tmp_path, monkeypatch)
    install_course_inventory(monkeypatch, [resource_module()])
    filtered_config = {
        **config,
        "filters.exclude_modules": [str(MODULE_ID)],
    }

    current = make_context(filtered_config)
    current.session = FakeSession()
    sync.sync(current)

    assert current.stats.failed == 0
    assert current.removed_content == set()


def test_unchanged_filter_newly_matching_content_suppresses_removal(
    tmp_path,
    monkeypatch,
):
    config = {
        "paths.sync_directory": str(tmp_path),
        "filters.exclude_modules": ["Secret*"],
    }
    install_course_inventory(monkeypatch, [resource_module()])
    baseline = make_context(config)
    baseline.session = FakeSession()
    sync.sync(baseline)
    course_cache.cache_root_node(baseline)
    baseline_scope = cached_course_payload(config)[
        course_cache.INVENTORY_SCOPE_CACHE_KEY
    ]

    renamed = resource_module()
    renamed["name"] = "Secret notes"
    install_course_inventory(monkeypatch, [renamed])
    current = make_context(config)
    current.session = FakeSession()
    sync.sync(current)

    assert current.inventory_filtered_course_ids == {COURSE_ID}
    assert current.removed_content == set()
    course_cache.cache_root_node(current)
    assert (
        cached_course_payload(config)[course_cache.INVENTORY_SCOPE_CACHE_KEY]
        == baseline_scope
    )


def test_malformed_nested_folder_inventory_suppresses_removal(
    tmp_path,
    monkeypatch,
):
    config = {"paths.sync_directory": str(tmp_path)}
    valid_file = resource_module()["contents"]
    install_course_inventory(monkeypatch, [folder_module(valid_file)])
    monkeypatch.setattr(
        moodle,
        "get_folders_by_courses",
        lambda *args: [{"coursemodule": MODULE_ID}],
    )
    baseline = make_context(config)
    baseline.session = FakeSession()
    sync.sync(baseline)
    course_cache.cache_root_node(baseline)

    install_course_inventory(monkeypatch, [folder_module(["malformed-file"])])
    current = make_context(config)
    current.session = FakeSession()
    sync.sync(current)

    assert current.stats.failed == 1
    assert current.incomplete_course_ids == {COURSE_ID}
    assert current.removed_content == set()


def test_missing_folder_details_suppress_intro_link_removal(tmp_path, monkeypatch):
    config = {"paths.sync_directory": str(tmp_path)}
    valid_file = resource_module()["contents"]
    install_course_inventory(monkeypatch, [folder_module(valid_file)])
    monkeypatch.setattr(
        moodle,
        "get_folders_by_courses",
        lambda *args: [
            {
                "coursemodule": MODULE_ID,
                "intro": "https://youtu.be/abcdefghijk",
            }
        ],
    )
    baseline = make_context(config)
    baseline.session = FakeSession()
    sync.sync(baseline)
    course_cache.cache_root_node(baseline)

    monkeypatch.setattr(moodle, "get_folders_by_courses", lambda *args: [])
    current = make_context(config)
    current.session = FakeSession()
    sync.sync(current)

    assert current.stats.failed == 1
    assert current.incomplete_course_ids == {COURSE_ID}
    assert current.removed_content == set()


def test_folder_details_are_not_required_when_link_following_is_disabled(
    tmp_path,
    monkeypatch,
):
    install_course_inventory(
        monkeypatch,
        [folder_module(resource_module()["contents"])],
    )
    folder_calls = []
    monkeypatch.setattr(
        moodle,
        "get_folders_by_courses",
        lambda *args: folder_calls.append(args) or None,
    )
    current = make_context(
        {
            "paths.sync_directory": str(tmp_path),
            "links.follow_links": False,
        }
    )
    current.session = FakeSession()

    sync.sync(current)

    assert folder_calls == []
    assert current.stats.failed == 0
    assert current.incomplete_course_ids == set()


def _prepared_course_with_download(
    url: str | None,
    *,
    download_kind: DownloadKind = DownloadKind.DIRECT,
) -> tuple[sync._PreparedCourse, Node]:
    course = Node("Download Course", COURSE_ID, "Course", None)
    section = course.add_child("General", 401, "Section")
    if url is not None:
        section.add_download_child(
            "recording.mp4",
            "episode-123" if download_kind is DownloadKind.OPENCAST else url,
            "Opencast" if download_kind is DownloadKind.OPENCAST else "Resource",
            url=url,
            download_kind=download_kind,
        )
    return sync._PreparedCourse("Download Course", COURSE_ID, course), course


@pytest.mark.parametrize(
    "download_kind",
    [DownloadKind.DIRECT, DownloadKind.OPENCAST],
)
def test_signed_url_rotation_preserves_remote_identity(download_kind):
    old_url = (
        "https://cdn.example.test/tracks/presentation.mp4?quality=hd&"
        "X-Amz-Credential=old-credential&X-Amz-Date=20260716T100000Z&"
        "X-Amz-Expires=60&X-Amz-Signature=old-signature"
    )
    new_url = (
        "https://cdn.example.test/tracks/presentation.mp4?"
        "X-Amz-Signature=new-signature&X-Amz-Expires=120&quality=hd&"
        "X-Amz-Date=20260716T110000Z&X-Amz-Credential=new-credential"
    )
    _, old_course = _prepared_course_with_download(
        old_url,
        download_kind=download_kind,
    )
    current, _ = _prepared_course_with_download(
        new_url,
        download_kind=download_kind,
    )

    assert sync._removed_course_content(current, old_course) == set()


def test_opencast_identity_distinguishes_tracks_within_one_episode():
    presentation_url = (
        "https://cdn.example.test/tracks/presentation.mp4?X-Amz-Signature=old"
    )
    presenter_url = "https://cdn.example.test/tracks/presenter.mp4?X-Amz-Signature=old"
    _, old_course = _prepared_course_with_download(
        presentation_url,
        download_kind=DownloadKind.OPENCAST,
    )
    old_course.children[0].add_download_child(
        "recording (presenter).mp4",
        "episode-123",
        "Opencast",
        url=presenter_url,
        download_kind=DownloadKind.OPENCAST,
    )
    current, _ = _prepared_course_with_download(
        "https://cdn.example.test/tracks/presentation.mp4?X-Amz-Signature=new",
        download_kind=DownloadKind.OPENCAST,
    )

    (removed,) = sync._removed_course_content(current, old_course)

    assert removed.remote_identity.endswith(
        ":https://cdn.example.test/tracks/presenter.mp4"
    )


def test_reported_signed_url_identity_does_not_leak_credentials():
    signed_url = (
        "https://cdn.example.test/file.pdf?X-Amz-Credential=aws-secret&"
        "X-Amz-Signature=signature-secret&"
        "X-Amz-Security-Token=session-secret&X-Amz-Expires=60"
    )
    _, old_course = _prepared_course_with_download(signed_url)
    current, _ = _prepared_course_with_download(None)

    (removed,) = sync._removed_course_content(current, old_course)

    assert "aws-secret" not in removed.remote_identity
    assert "signature-secret" not in removed.remote_identity
    assert "session-secret" not in removed.remote_identity
    assert removed.remote_identity.count("[REDACTED]") == 3


def test_downloader_only_url_filter_does_not_truncate_inventory(tmp_path):
    context = make_context({"filters.exclude_links": ["*notes.pdf*"]})
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    course = semester.add_child("Download Course", COURSE_ID, "Course")
    file_node = course.add_download_child(
        "notes.pdf",
        RESOURCE_URL,
        "Resource",
        url=RESOURCE_URL,
    )

    outcome = downloader.should_skip_before_decision(
        context,
        file_node,
        tmp_path / "notes.pdf",
    )

    assert outcome is not None
    assert context.inventory_filtered_course_ids == set()


def test_cache_without_inventory_scope_is_not_guessed_authoritative(
    tmp_path,
    monkeypatch,
):
    config = {"paths.sync_directory": str(tmp_path)}
    seeded = make_context(config)
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    course = semester.add_child("Download Course", COURSE_ID, "Course")
    section = course.add_child("General", 401, "Section")
    section.add_download_child(
        "notes.pdf",
        RESOURCE_URL,
        "Resource",
        url=RESOURCE_URL,
    )
    seeded.root_node = root
    course_cache.cache_root_node(seeded)
    install_course_inventory(monkeypatch, [])

    current = make_context(config)
    current.session = FakeSession()
    sync.sync(current)

    assert current.removed_content == set()
