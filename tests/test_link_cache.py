import pytest

from syncmymoodle import course_cache, links
from syncmymoodle.constants import LINKED_PAGE_MAX_BYTES
from syncmymoodle.context import MoodleAccount
from syncmymoodle.moodle_tokens import MoodleTokens
from syncmymoodle.node import Node

from .helpers import FakeResponse, FakeSession, make_context

LINK_URL = "https://links.example.test/overview"
YOUTUBE_ONE = "https://youtu.be/abcdefghijk"
YOUTUBE_TWO = "https://youtu.be/lmnopqrstuv"
HTML_ONE = f'<a href="{YOUTUBE_ONE}">first</a>'
HTML_TWO = f'<a href="{YOUTUBE_TWO}">second</a>'


def _context(tmp_path, *, user_id=10001, extra_config=None):
    config = {
        "paths.sync_directory": str(tmp_path),
        "links.youtube": True,
        "links.opencast": False,
        "links.sciebo": False,
        "links.emedia": False,
        **(extra_config or {}),
    }
    ctx = make_context(config)
    ctx.moodle_account = MoodleAccount(
        MoodleTokens(
            "fake-user",
            "fake-webservice-token",
            "fake-private-token",
            moodle_user_id=user_id,
        )
    )
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    course = semester.add_child("Linked Course", 101, "Course")
    section = course.add_child("General", 201, "Section")
    ctx.root_node = root
    course_cache.get_course_cache_root(ctx, course)
    return ctx, course, section


def _seed_html(tmp_path, headers=None, html=HTML_ONE):
    ctx, _, section = _context(tmp_path)
    session = FakeSession()
    session.add(
        "HEAD",
        LINK_URL,
        FakeResponse(headers={"Content-Type": "text/html"}),
    )
    session.add(
        "GET",
        LINK_URL,
        FakeResponse(
            text=html,
            headers={"Content-Type": "text/html", **(headers or {})},
        ),
    )
    ctx.session = session

    links.scan_for_links(ctx, LINK_URL, section, 101, single=True)
    course_cache.cache_root_node(ctx)

    assert session.calls == [("HEAD", LINK_URL), ("GET", LINK_URL)]
    return ctx


def _youtube_ids(parent):
    return [child.id for child in parent.children if child.type == "Youtube"]


@pytest.mark.parametrize(
    ("response_header", "request_header", "value"),
    [
        ("ETag", "If-None-Match", '"page-v1"'),
        (
            "Last-Modified",
            "If-Modified-Since",
            "Wed, 15 Jul 2026 10:00:00 GMT",
        ),
    ],
)
def test_cached_html_is_revalidated_and_rescanned(
    tmp_path, response_header, request_header, value
):
    _seed_html(tmp_path, {response_header: value})
    ctx, _, section = _context(tmp_path)
    session = FakeSession()

    def not_modified(url, kwargs):
        assert kwargs["headers"] == {request_header: value}
        assert kwargs["stream"] is True
        return FakeResponse(status_code=304)

    session.add("GET", LINK_URL, not_modified)
    ctx.session = session

    links.scan_for_links(ctx, LINK_URL, section, 101, single=True)

    assert session.calls == [("GET", LINK_URL)]
    assert _youtube_ids(section) == ["abcdefghijk"]


def test_changed_html_replaces_the_persisted_cache(tmp_path):
    _seed_html(tmp_path, {"ETag": '"page-v1"'})
    changed, _, changed_section = _context(tmp_path)
    changed_session = FakeSession()

    def changed_page(url, kwargs):
        assert kwargs["headers"] == {"If-None-Match": '"page-v1"'}
        return FakeResponse(
            text=HTML_TWO,
            headers={"Content-Type": "text/html", "ETag": '"page-v2"'},
        )

    changed_session.add("GET", LINK_URL, changed_page)
    changed.session = changed_session
    links.scan_for_links(changed, LINK_URL, changed_section, 101, single=True)
    course_cache.cache_root_node(changed)

    current, _, current_section = _context(tmp_path)
    current_session = FakeSession()

    def current_page(url, kwargs):
        assert kwargs["headers"] == {"If-None-Match": '"page-v2"'}
        return FakeResponse(status_code=304)

    current_session.add("GET", LINK_URL, current_page)
    current.session = current_session
    links.scan_for_links(current, LINK_URL, current_section, 101, single=True)

    assert _youtube_ids(changed_section) == ["lmnopqrstuv"]
    assert _youtube_ids(current_section) == ["lmnopqrstuv"]


def test_cache_control_max_age_skips_fresh_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(links.time, "time", lambda: 1_000.0)
    _seed_html(tmp_path, {"Cache-Control": "private, max-age=3600"})

    monkeypatch.setattr(links.time, "time", lambda: 1_001.0)
    ctx, _, section = _context(tmp_path)
    session = FakeSession()
    ctx.session = session

    links.scan_for_links(ctx, LINK_URL, section, 101, single=True)

    assert session.calls == []
    assert _youtube_ids(section) == ["abcdefghijk"]


def test_cached_html_without_validators_is_fetched_without_a_head(tmp_path):
    _seed_html(tmp_path)
    ctx, _, section = _context(tmp_path)
    session = FakeSession()

    def current_page(url, kwargs):
        assert kwargs["headers"] == {}
        return FakeResponse(text=HTML_ONE, headers={"Content-Type": "text/html"})

    session.add("GET", LINK_URL, current_page)
    ctx.session = session

    links.scan_for_links(ctx, LINK_URL, section, 101, single=True)

    assert session.calls == [("GET", LINK_URL)]
    assert _youtube_ids(section) == ["abcdefghijk"]


def test_identical_links_are_requested_once_per_run(tmp_path):
    ctx, course, first_section = _context(tmp_path)
    second_section = course.add_child("More", 202, "Section")
    session = FakeSession()
    session.add(
        "HEAD",
        LINK_URL,
        FakeResponse(headers={"Content-Type": "text/html"}),
    )
    session.add(
        "GET",
        LINK_URL,
        FakeResponse(text=HTML_ONE, headers={"Content-Type": "text/html"}),
    )
    ctx.session = session

    links.scan_for_links(ctx, LINK_URL, first_section, 101, single=True)
    links.scan_for_links(ctx, LINK_URL, second_section, 101, single=True)

    assert session.calls == [("HEAD", LINK_URL), ("GET", LINK_URL)]
    assert _youtube_ids(first_section) == ["abcdefghijk"]
    assert _youtube_ids(second_section) == ["abcdefghijk"]


def test_reused_link_result_obeys_per_course_domain_filter(tmp_path):
    final_url = "https://files.example.test/document.pdf"
    ctx, course, first_section = _context(
        tmp_path,
        extra_config={
            "filters.allowed_domains": {
                "101": ["links.example.test", "files.example.test"],
                "202": ["links.example.test"],
            }
        },
    )
    second_section = course.add_child("Other course policy", 202, "Section")
    session = FakeSession()
    session.add(
        "HEAD",
        LINK_URL,
        FakeResponse(status_code=302, headers={"Location": final_url}),
    )
    session.add(
        "HEAD",
        final_url,
        FakeResponse(headers={"Content-Type": "application/pdf"}),
    )
    ctx.session = session

    links.scan_for_links(ctx, LINK_URL, first_section, 101, single=True)
    links.scan_for_links(ctx, LINK_URL, second_section, 202, single=True)

    assert session.calls == [("HEAD", LINK_URL), ("HEAD", final_url)]
    assert len(first_section.children) == 1
    assert second_section.children == []
    assert {item.config_key for item in ctx.filtered_items} == {
        "filters.allowed_domains"
    }


def test_policy_rejection_does_not_poison_later_permissive_course(tmp_path):
    final_url = "https://files.example.test/document.pdf"
    ctx, course, restrictive_section = _context(
        tmp_path,
        extra_config={
            "filters.allowed_domains": {
                "101": ["links.example.test"],
                "202": ["links.example.test", "files.example.test"],
            }
        },
    )
    permissive_section = course.add_child("Permissive course", 202, "Section")
    session = FakeSession()
    session.add(
        "HEAD",
        LINK_URL,
        FakeResponse(status_code=302, headers={"Location": final_url}),
    )
    session.add(
        "HEAD",
        final_url,
        FakeResponse(headers={"Content-Type": "application/pdf"}),
    )
    ctx.session = session

    links.scan_for_links(ctx, LINK_URL, restrictive_section, 101, single=True)
    links.scan_for_links(ctx, LINK_URL, permissive_section, 202, single=True)

    assert session.calls == [
        ("HEAD", LINK_URL),
        ("HEAD", LINK_URL),
        ("HEAD", final_url),
    ]
    assert restrictive_section.children == []
    assert len(permissive_section.children) == 1


@pytest.mark.parametrize("declared", [True, False])
def test_oversized_linked_html_is_not_read_or_cached(tmp_path, declared):
    ctx, _, section = _context(tmp_path)
    session = FakeSession()
    session.add(
        "HEAD",
        LINK_URL,
        FakeResponse(headers={"Content-Type": "text/html"}),
    )
    body = b"<html>" + b"x" * LINKED_PAGE_MAX_BYTES
    response = FakeResponse(
        headers={
            "Content-Type": "text/html",
            **({"Content-Length": str(len(body))} if declared else {}),
        },
        chunks=[body],
    )
    session.add("GET", LINK_URL, response)
    ctx.session = session

    links.scan_for_links(ctx, LINK_URL, section, 101, single=True)

    assert section.children == []
    assert LINK_URL not in ctx.linked_resource_results
    assert LINK_URL not in ctx.linked_resources_by_course["101"]


def test_no_store_responses_are_not_persisted(tmp_path):
    _seed_html(tmp_path, {"Cache-Control": "no-store", "ETag": '"private"'})
    ctx, _, section = _context(tmp_path)
    session = FakeSession()
    session.add(
        "HEAD",
        LINK_URL,
        FakeResponse(headers={"Content-Type": "text/html"}),
    )
    session.add(
        "GET",
        LINK_URL,
        FakeResponse(text=HTML_ONE, headers={"Content-Type": "text/html"}),
    )
    ctx.session = session

    links.scan_for_links(ctx, LINK_URL, section, 101, single=True)

    assert session.calls == [("HEAD", LINK_URL), ("GET", LINK_URL)]


def test_link_cache_is_bound_to_the_moodle_account(tmp_path):
    _seed_html(tmp_path, {"ETag": '"account-one"'})
    ctx, _, section = _context(tmp_path, user_id=20002)
    session = FakeSession()
    session.add(
        "HEAD",
        LINK_URL,
        FakeResponse(headers={"Content-Type": "text/html"}),
    )

    def other_account_page(url, kwargs):
        assert kwargs["headers"] == {}
        return FakeResponse(text=HTML_ONE, headers={"Content-Type": "text/html"})

    session.add("GET", LINK_URL, other_account_page)
    ctx.session = session

    links.scan_for_links(ctx, LINK_URL, section, 101, single=True)

    assert session.calls == [("HEAD", LINK_URL), ("GET", LINK_URL)]


def test_cached_direct_file_keeps_one_freshness_check(tmp_path):
    ctx, _, section = _context(tmp_path)
    session = FakeSession()
    session.add(
        "HEAD",
        LINK_URL,
        FakeResponse(
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": "1234",
                "ETag": '"file-v1"',
            }
        ),
    )
    ctx.session = session
    links.scan_for_links(ctx, LINK_URL, section, 101, single=True)
    course_cache.cache_root_node(ctx)

    current, _, current_section = _context(tmp_path)
    current_session = FakeSession()

    def not_modified(url, kwargs):
        assert kwargs["headers"] == {"If-None-Match": '"file-v1"'}
        return FakeResponse(status_code=304)

    current_session.add("HEAD", LINK_URL, not_modified)
    current.session = current_session
    links.scan_for_links(current, LINK_URL, current_section, 101, single=True)

    assert current_session.calls == [("HEAD", LINK_URL)]
    assert len(current_section.children) == 1
    file_node = current_section.children[0]
    assert file_node.etag == '"file-v1"'
    assert file_node.remote_size == 1234


def test_known_provider_links_skip_generic_requests(tmp_path):
    ctx = make_context(
        {
            "paths.sync_directory": str(tmp_path),
            "links.youtube": False,
            "links.opencast": False,
            "links.sciebo": False,
            "links.emedia": False,
        }
    )
    session = FakeSession()
    ctx.session = session
    parent = Node("Section", 1, "Section", None)
    provider_urls = [
        YOUTUBE_ONE,
        "https://engage.streaming.rwth-aachen.de/play/"
        "11111111-2222-4333-8444-555555555555",
        "https://rwth-aachen.sciebo.de/s/share-token-123",
        "https://emedia-medizin.rwth-aachen.de/web/veira_fe/#/watch/864",
    ]

    for url in provider_urls:
        links.scan_for_links(ctx, url, parent, 101, single=True)

    assert session.calls == []
    assert parent.children == []

    youtube_ctx = make_context({"links.youtube": True})
    youtube_ctx.session = FakeSession()
    youtube_parent = Node("Section", 1, "Section", None)
    links.scan_for_links(youtube_ctx, YOUTUBE_ONE, youtube_parent, 101, single=True)
    assert _youtube_ids(youtube_parent) == ["abcdefghijk"]
    assert youtube_ctx.session.calls == []


def test_conditional_headers_are_stripped_from_cross_origin_redirects(tmp_path):
    original_url = "https://links.example.test/redirect"
    final_url = "https://pages.example.test/overview"
    config = {"filters.allowed_domains": ["links.example.test", "pages.example.test"]}
    seeded, _, seeded_section = _context(tmp_path, extra_config=config)
    seeded_session = FakeSession()
    seeded_session.add(
        "HEAD",
        original_url,
        FakeResponse(status_code=302, headers={"Location": final_url}),
    )
    seeded_session.add(
        "HEAD",
        final_url,
        FakeResponse(headers={"Content-Type": "text/html"}),
    )
    seeded_session.add(
        "GET",
        final_url,
        FakeResponse(
            text="<html></html>",
            headers={"Content-Type": "text/html", "ETag": '"redirect-v1"'},
        ),
    )
    seeded.session = seeded_session
    links.scan_for_links(seeded, original_url, seeded_section, 101, single=True)
    course_cache.cache_root_node(seeded)

    current, _, current_section = _context(tmp_path, extra_config=config)
    current_session = FakeSession()

    def redirect(url, kwargs):
        assert kwargs["headers"] == {"If-None-Match": '"redirect-v1"'}
        return FakeResponse(status_code=302, headers={"Location": final_url})

    def final_page(url, kwargs):
        assert "If-None-Match" not in kwargs["headers"]
        assert "If-Modified-Since" not in kwargs["headers"]
        return FakeResponse(
            text="<html></html>",
            headers={"Content-Type": "text/html", "ETag": '"redirect-v1"'},
        )

    current_session.add("GET", original_url, redirect)
    current_session.add("GET", final_url, final_page)
    current.session = current_session

    links.scan_for_links(current, original_url, current_section, 101, single=True)

    assert current_session.calls == [("GET", original_url), ("GET", final_url)]
