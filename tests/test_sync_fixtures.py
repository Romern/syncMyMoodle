import io
import logging
import zipfile

import requests

from syncmymoodle import links, opencast, sciebo, sync, sync_handlers
from syncmymoodle.constants import HTTP_TIMEOUT_SECONDS
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


def run_h5p_handler(monkeypatch, response):
    ctx = make_context()
    session = FakeSession()
    session.add("GET", H5P_PACKAGE_URL, response)
    ctx.session = session
    monkeypatch.setattr(
        sync_handlers.moodle_api,
        "get_h5pactivities_by_course",
        lambda session, wstoken, course_id: [
            {
                "coursemodule": 317,
                "package": [{"fileurl": H5P_PACKAGE_URL}],
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

    section_node = run_h5p_handler(
        monkeypatch,
        FakeResponse(content=package.getvalue()),
    )

    assert section_node.children == []
    assert "H5P content for module 317 is too large" in caplog.text


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


def test_assignment_intro_opencast_embed_is_added_to_assignment_node(monkeypatch):
    courses = [load_json_fixture("moodle", "courses.json")[1]]
    syncer = make_context(
        {
            "modules.assignment": True,
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
        {102: load_json_fixture("moodle", "assignment_opencast_course.json")},
        {102: load_json_fixture("moodle", "assignment_opencast_assignments.json")},
    )
    syncer.session = FakeSession()

    authenticated = []
    monkeypatch.setattr(
        opencast,
        "authenticate_episode",
        lambda ctx, course_id, episode_id, *a, **k: (
            authenticated.append((course_id, episode_id)) or True
        ),
    )
    monkeypatch.setattr(
        opencast,
        "resolve_tracks_from_episode",
        lambda ctx, episode_id, *a, **k: (
            opencast.OpencastTrack(
                f"https://video.example.test/{episode_id}/presentation.mp4",
                checksum_type="md5",
                checksum="11111111111111111111111111111111",
                flavor_type="presentation",
            ),
        ),
    )

    sync.sync(syncer)

    assert authenticated == [(102, "11111111-2222-4333-8444-555555555555")]
    assert_snapshot("assignment_opencast_tree.txt", node_rows(syncer.root_node))


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

    episodes = sync_handlers._opencast_series_episodes(
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

    episodes = sync_handlers._opencast_series_episodes(
        syncer,
        "series-123",
        logging.getLogger("test"),
    )

    assert episodes == [
        (f"episode-{index}", f"Episode {index}") for index in range(100)
    ]
    assert requested_urls == [
        f"{opencast.OPENCAST_SEARCH_URL}?limit=100&offset=0&sid=series-123",
        f"{opencast.OPENCAST_SEARCH_URL}?limit=100&offset=100&sid=series-123",
    ]
    assert "made no pagination progress at offset 100" in caplog.text


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
        "authenticate_episode",
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
    series_episode_a = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    series_episode_b = "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa"
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
        FakeResponse(json_payload=load_json_fixture("opencast", "series.json")),
    )
    session.add(
        "GET",
        "https://engage.streaming.rwth-aachen.de/search/episode.json"
        f"?id={series_episode_a}",
        FakeResponse(
            json_payload=load_json_fixture("opencast", "episode_series_a.json")
        ),
    )
    session.add(
        "GET",
        "https://engage.streaming.rwth-aachen.de/search/episode.json"
        f"?id={series_episode_b}",
        FakeResponse(
            json_payload=load_json_fixture("opencast", "episode_series_b.json")
        ),
    )
    syncer.session = session

    sync.sync(syncer)

    assert launch_calls == [9001, 9002]
    assert session.count("POST", lti_submit_url) == 2
    assert_snapshot("opencast_lti_tree.txt", node_rows(syncer.root_node))
