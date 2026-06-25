from syncmymoodle.__main__ import Node

from .helpers import (
    FakeResponse,
    FakeSession,
    assert_snapshot,
    install_moodle_fixtures,
    load_fixture,
    load_json_fixture,
    make_syncer,
    node_rows,
)


def test_nested_moodle_folder_paths_are_preserved():
    courses = [load_json_fixture("moodle", "courses.json")[0]]
    syncer = make_syncer()
    install_moodle_fixtures(
        syncer,
        courses,
        {101: load_json_fixture("moodle", "nested_folder_course.json")},
    )
    syncer.session = FakeSession()

    syncer.sync()

    assert_snapshot("nested_folder_tree.txt", node_rows(syncer.root_node))


def test_assignment_intro_opencast_embed_is_added_to_assignment_node():
    courses = [load_json_fixture("moodle", "courses.json")[1]]
    syncer = make_syncer(
        {
            "used_modules": {
                "assign": True,
                "resource": False,
                "url": {"youtube": False, "opencast": True, "sciebo": False},
                "folder": False,
            }
        }
    )
    install_moodle_fixtures(
        syncer,
        courses,
        {102: load_json_fixture("moodle", "assignment_opencast_course.json")},
        {102: load_json_fixture("moodle", "assignment_opencast_assignments.json")},
    )
    syncer.session = FakeSession()

    authenticated = []
    syncer._authenticate_opencast_episode = (  # type: ignore[method-assign]
        lambda course_id, episode_id: authenticated.append((course_id, episode_id))
        or True
    )
    syncer.extractTrackFromEpisode = (  # type: ignore[method-assign]
        lambda episode_id: f"https://video.example.test/{episode_id}/presentation.mp4"
    )

    syncer.sync()

    assert authenticated == [(102, "11111111-2222-4333-8444-555555555555")]
    assert_snapshot("assignment_opencast_tree.txt", node_rows(syncer.root_node))


def test_skip_rules_apply_to_sections_modules_links_and_domains():
    courses = [load_json_fixture("moodle", "courses.json")[2]]
    syncer = make_syncer(
        {
            "exclude_sections": {"*": ["Hidden*"]},
            "exclude_modules": {"103": ["Skip Module"]},
            "exclude_links": ["*excluded.pdf"],
            "allowed_domains": ["moodle.rwth-aachen.de"],
            "used_modules": {
                "assign": False,
                "resource": True,
                "url": {"youtube": False, "opencast": False, "sciebo": False},
                "folder": False,
            },
        }
    )
    install_moodle_fixtures(
        syncer,
        courses,
        {103: load_json_fixture("moodle", "skip_rules_course.json")},
    )
    syncer.session = FakeSession()

    syncer.sync()

    assert_snapshot("skip_rules_tree.txt", node_rows(syncer.root_node))


def test_sciebo_public_share_is_cached_per_sync_run():
    link = "https://rwth-aachen.sciebo.de/s/share-token-123"
    public_root = "https://rwth-aachen.sciebo.de/public.php/webdav/"
    public_slides = "https://rwth-aachen.sciebo.de/public.php/webdav/slides/"
    syncer = make_syncer(
        {
            "used_modules": {
                "assign": False,
                "resource": False,
                "url": {"youtube": False, "opencast": False, "sciebo": True},
                "folder": False,
            }
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

    root = Node("", -1, "Root", None)
    first_parent = root.add_child("First occurrence", 1, "Section")
    second_parent = root.add_child("Second occurrence", 2, "Section")

    syncer.scanForLinks(link, first_parent, 101)
    syncer.scanForLinks(link, second_parent, 101)

    assert session.count("GET", link) == 1
    assert session.count("PROPFIND", public_root) == 1
    assert session.count("PROPFIND", public_slides) == 1
    assert [
        row.replace("First occurrence/", "") for row in node_rows(first_parent)
    ] == [row.replace("Second occurrence/", "") for row in node_rows(second_parent)]
    assert node_rows(first_parent) == [
        "Sciebo Folder | First occurrence/sciebo-share-token-123 |  |  | ",
        "Sciebo File | First occurrence/sciebo-share-token-123/readme.pdf | "
        "https://rwth-aachen.sciebo.de/public.php/webdav/readme.pdf |  | "
        "1111111111111111111111111111111111111111",
        "Sciebo Folder | First occurrence/sciebo-share-token-123/slides |  |  | "
        '"folder-slides"',
        "Sciebo File | First occurrence/sciebo-share-token-123/slides/deck.pdf | "
        "https://rwth-aachen.sciebo.de/public.php/webdav/slides/deck.pdf |  | "
        "2222222222222222222222222222222222222222",
    ]


def test_mixed_course_sync_tree_covers_common_module_surfaces():
    courses = load_json_fixture("moodle", "mixed_courses.json")
    direct_pdf = "https://files.example.test/direct.pdf"
    html_overview = "https://files.example.test/overview.html"
    page_url = "https://moodle.rwth-aachen.de/mod/page/view.php?id=315"
    h5p_url = "https://moodle.rwth-aachen.de/mod/h5pactivity/view.php?id=317"
    h5p_iframe_url = "https://moodle.rwth-aachen.de/h5p/embed/317"
    syncer = make_syncer(
        {
            "used_modules": {
                "assign": True,
                "resource": True,
                "url": {"youtube": True, "opencast": True, "sciebo": False},
                "folder": True,
            }
        }
    )
    install_moodle_fixtures(
        syncer,
        courses,
        {104: load_json_fixture("moodle", "mixed_course.json")},
        {104: load_json_fixture("moodle", "mixed_assignments.json")},
        {412: load_json_fixture("moodle", "mixed_submission_files.json")},
        {104: load_json_fixture("moodle", "mixed_folders.json")},
    )
    session = FakeSession()
    session.add(
        "HEAD",
        direct_pdf,
        FakeResponse(headers={"Content-Type": "application/pdf"}),
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
    session.add(
        "GET", h5p_url, FakeResponse(text=load_fixture("html", "h5p_view.html"))
    )
    session.add(
        "GET",
        h5p_iframe_url,
        FakeResponse(text=load_fixture("html", "h5p_iframe.html")),
    )
    syncer.session = session
    syncer._authenticate_opencast_episode = lambda course_id, episode_id: True  # type: ignore[method-assign]
    syncer.extractTrackFromEpisode = lambda episode_id: (  # type: ignore[method-assign]
        f"https://video.example.test/{episode_id}/presentation.mp4"
    )

    syncer.sync()

    assert session.count("HEAD", direct_pdf) == 1
    assert session.count("GET", direct_pdf) == 0
    assert session.count("HEAD", html_overview) == 1
    assert session.count("GET", html_overview) == 1
    assert session.count("GET", page_url) == 1
    assert session.count("GET", h5p_url) == 1
    assert session.count("GET", h5p_iframe_url) == 1
    assert_snapshot("mixed_course_tree.txt", node_rows(syncer.root_node))


def test_opencast_lti_single_and_series_use_lti_and_api_routes():
    courses = [load_json_fixture("moodle", "courses.json")[0]]
    single_lti_url = (
        "https://moodle.rwth-aachen.de/mod/lti/launch.php?id=501&triggerview=0"
    )
    series_lti_url = (
        "https://moodle.rwth-aachen.de/mod/lti/launch.php?id=502&triggerview=0"
    )
    lti_submit_url = "https://engage.streaming.rwth-aachen.de/lti"
    single_episode = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    series_id = "series-1111-2222"
    series_episode_a = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    series_episode_b = "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa"
    series_url = (
        "https://engage.streaming.rwth-aachen.de/search/episode.json"
        f"?limit=100&offset=0&sid={series_id}"
    )
    syncer = make_syncer(
        {
            "used_modules": {
                "assign": False,
                "resource": False,
                "url": {"youtube": False, "opencast": True, "sciebo": False},
                "folder": False,
            }
        }
    )
    install_moodle_fixtures(
        syncer,
        courses,
        {101: load_json_fixture("moodle", "opencast_lti_course.json")},
    )
    session = FakeSession()
    session.add(
        "GET",
        single_lti_url,
        FakeResponse(text=load_fixture("opencast", "lti_single.html")),
    )
    session.add(
        "GET",
        series_lti_url,
        FakeResponse(text=load_fixture("opencast", "lti_series.html")),
    )
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

    syncer.sync()

    assert session.count("GET", single_lti_url) == 1
    assert session.count("GET", series_lti_url) == 1
    assert session.count("POST", lti_submit_url) == 2
    assert_snapshot("opencast_lti_tree.txt", node_rows(syncer.root_node))
