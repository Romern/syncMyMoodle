from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from syncmymoodle.config import Config
from syncmymoodle.constants import HTTP_TIMEOUT_SECONDS
from syncmymoodle.context import MoodleAccount, SyncContext
from syncmymoodle.moodle_tokens import MoodleTokens
from syncmymoodle.node import Node
from syncmymoodle.pathing import get_sanitized_node_path

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOTS = Path(__file__).parent / "snapshots"

TEST_CONFIG_OVERRIDES = {
    "modules.quiz": "off",
}


class FakeKeyring:
    def __init__(self, values: dict[tuple[str, str], str] | None = None) -> None:
        self.values = {} if values is None else values

    def get_keyring(self) -> object:
        return object()

    def get_password(self, service: str, name: str) -> str | None:
        return self.values.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.values[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        self.values.pop((service, name), None)


@dataclass
class FakeResponse:
    text: str = ""
    content: bytes | None = None
    encoding: str | None = None
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    json_payload: Any = None
    chunks: list[bytes] | None = None

    def json(self) -> Any:
        if self.json_payload is not None:
            return self.json_payload
        return json.loads(self.text)

    def iter_content(self, block_size: int):
        del block_size
        if self.chunks is not None:
            yield from self.chunks
            return
        body = self.content
        if body is None and self.text:
            body = self.text.encode(self.encoding or "utf-8")
        if body:
            yield body

    def close(self) -> None:
        pass


RouteResult = FakeResponse | Callable[[str, dict[str, Any]], FakeResponse]


class FakeSession:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], RouteResult] = {}
        self.calls: list[tuple[str, str]] = []
        self.headers: dict[str, str] = {}
        self.cookies: Any = []

    def add(self, method: str, url: str, response: RouteResult) -> None:
        self.routes[(method.upper(), url)] = response

    def count(self, method: str, url: str | None = None) -> int:
        method = method.upper()
        if url is None:
            return sum(1 for call_method, _ in self.calls if call_method == method)
        return Counter(self.calls)[(method, url)]

    def _dispatch(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        method = method.upper()
        assert kwargs.get("timeout") == HTTP_TIMEOUT_SECONDS, (
            f"Fake HTTP request must use the shared timeout: {method} {url}"
        )
        self.calls.append((method, url))
        route = self.routes.get((method, url))
        if route is None:
            raise AssertionError(f"Unexpected fake HTTP request: {method} {url}")
        response = route(url, kwargs) if callable(route) else route
        if response.url is None:
            response.url = url
        return response

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("GET", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("HEAD", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch(method, url, **kwargs)


def load_fixture(*parts: str) -> str:
    return (FIXTURES.joinpath(*parts)).read_text(encoding="utf-8")


def load_json_fixture(*parts: str) -> Any:
    return json.loads(load_fixture(*parts))


def load_snapshot(name: str) -> list[str]:
    return [
        line
        for line in (SNAPSHOTS / name).read_text(encoding="utf-8").splitlines()
        if line
    ]


def assert_snapshot(name: str, actual: list[str], *, update: bool = False) -> None:
    """Compare ``actual`` against a stored snapshot.

    Passing the explicit pytest ``--update-snapshots`` option rewrites the
    snapshot from ``actual``. Normal and CI runs always assert by default.
    """
    if update:
        path = SNAPSHOTS / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(actual) + "\n", encoding="utf-8")
        return
    assert actual == load_snapshot(name)


def make_context(config: dict[str, Any] | None = None) -> SyncContext:
    merged_config = TEST_CONFIG_OVERRIDES.copy()
    if config:
        merged_config.update(config)
    ctx = SyncContext(config=Config.from_dict(merged_config))
    ctx.moodle_account = MoodleAccount(
        MoodleTokens(
            "fake-user",
            "fake-webservice-token",
            "fake-private-token",
            moodle_user_id=10001,
        ),
    )
    ctx.session_key = "fake-sesskey"
    return ctx


def node_path(ctx: SyncContext, node: Node) -> Path:
    return get_sanitized_node_path(node, Path(ctx.config.sync_directory))


def node_at_path(root: Node, target_path: list[str]) -> Node:
    node = root
    for name in filter(None, target_path):
        child = next((child for child in node.children if child.name == name), None)
        assert child is not None, f"Node path not found: {'/'.join(target_path)}"
        node = child
    return node


def two_course_tree(
    *,
    with_cached_files: bool = False,
) -> tuple[Node, tuple[Node, Node], tuple[Node, Node]]:
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    courses = (
        semester.add_child("First Course", 101, "Course"),
        semester.add_child("Second Course", 202, "Course"),
    )
    sections = tuple(
        course.add_child("General", 1000 + int(course.id), "Section")
        for course in courses
    )
    if with_cached_files:
        for course, section in zip(courses, sections, strict=True):
            cached = section.add_download_child(
                f"cached-{course.id}.pdf",
                f"cached-{course.id}",
                "Resource",
                url=f"https://example.test/cached-{course.id}.pdf",
            )
            cached.mark_handled()
    return root, courses, sections


def install_moodle_fixtures(
    monkeypatch: Any,
    courses: list[dict[str, Any]],
    course_contents: dict[int, list[dict[str, Any]]],
    assignments: dict[int, dict[str, Any] | None] | None = None,
    submission_files: dict[int, list[dict[str, Any]]] | None = None,
    folders: dict[int, list[dict[str, Any]]] | None = None,
) -> None:
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_all_courses",
        lambda session, wstoken, user_id: courses,
    )
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_course",
        lambda session, wstoken, course_id: course_contents[int(course_id)],
    )
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_assignment",
        lambda session, wstoken, course_id: (assignments or {}).get(int(course_id)),
    )
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_folders_by_courses",
        lambda session, wstoken, course_id: (folders or {}).get(int(course_id), []),
    )
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_h5pactivities_by_course",
        lambda session, wstoken, course_id: [],
    )
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_quizzes_by_course",
        lambda session, wstoken, course_id: [],
    )
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_ltis_by_course",
        lambda session, wstoken, course_id: [],
    )
    # The assignment handler fetches submission files via the moodle module
    # directly, so stub it there (leak-safe via monkeypatch).
    monkeypatch.setattr(
        "syncmymoodle.moodle.get_assignment_submission_files",
        lambda session, wstoken, user_id, assignment_id, *a, **k: (
            submission_files or {}
        ).get(int(assignment_id), []),
    )


def node_rows(root: Node) -> list[str]:
    rows = []

    def walk(node: Node) -> None:
        for child in node.children:
            path = "/".join(part for part in child.get_path() if part)
            rows.append(
                " | ".join(
                    [
                        child.type,
                        path,
                        child.url or "",
                        str(child.timemodified or ""),
                        str(child.etag or ""),
                    ]
                ).rstrip()
            )
            walk(child)

    walk(root)
    return rows


def build_single_file_tree(
    filename: str,
    url: str,
    *,
    timemodified: int | None = None,
    etag: str | None = None,
    remote_size: int | None = None,
    semester: str = "26ss",
    course: str = "Download Course",
    course_id: int = 301,
    section: str = "General",
    section_id: int = 401,
    file_type: str = "Linked file [application/pdf]",
) -> tuple[Node, Node | None]:
    """Build a Root/Semester/Course/Section/<file> tree.

    Returns the root node and the leaf file node so tests can drive
    ``download_file`` against a realistic, course-scoped path (which the cache
    lookups in ``download_file`` rely on).
    """
    root = Node("", -1, "Root", None)
    semester_node = root.add_child(semester, None, "Semester")
    course_node = semester_node.add_child(course, course_id, "Course")
    section_node = course_node.add_child(section, section_id, "Section")
    file_node = section_node.add_child(
        filename,
        url,
        file_type,
        url=url,
        timemodified=timemodified,
        etag=etag,
        remote_size=remote_size,
    )
    return root, file_node
