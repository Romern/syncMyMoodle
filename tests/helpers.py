from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from syncmymoodle import downloader
from syncmymoodle.config import Config
from syncmymoodle.context import SyncContext
from syncmymoodle.node import Node
from syncmymoodle.pathing import get_sanitized_node_path

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOTS = Path(__file__).parent / "snapshots"

# Set SMM_UPDATE_SNAPSHOTS=1 to rewrite snapshot files from the actual rows
# instead of asserting against them. Use this after an intentional change to
# the sync behavior, then review the resulting snapshot diff.
UPDATE_SNAPSHOTS = os.environ.get("SMM_UPDATE_SNAPSHOTS") not in (None, "", "0")


DEFAULT_CONFIG = {
    "paths.sync_directory": "./",
    "courses.selected": [],
    "courses.skip": [],
    "courses.semesters": [],
    "courses.prefix_handling": "keep",
    "links.follow_links": True,
    "links.youtube": True,
    "links.opencast": True,
    "links.sciebo": True,
    "modules.assignment": True,
    "modules.resource": True,
    "modules.folder": True,
    "modules.quiz": "off",
    "filters.exclude_filetypes": [],
    "filters.exclude_files": [],
    "filters.exclude_links": [],
    "filters.allowed_domains": [],
    "filters.exclude_sections": [],
    "filters.exclude_modules": [],
    "downloads.update_files": False,
    "downloads.conflict_handling": "rename",
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
        yield from self.chunks or []

    def close(self) -> None:
        pass


RouteResult = FakeResponse | Callable[[str, dict[str, Any]], FakeResponse]


class FakeSession:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], RouteResult] = {}
        self.calls: list[tuple[str, str]] = []

    def add(self, method: str, url: str, response: RouteResult) -> None:
        self.routes[(method.upper(), url)] = response

    def count(self, method: str, url: str | None = None) -> int:
        method = method.upper()
        if url is None:
            return sum(1 for call_method, _ in self.calls if call_method == method)
        return Counter(self.calls)[(method, url)]

    def _dispatch(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        method = method.upper()
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


def assert_snapshot(name: str, actual: list[str]) -> None:
    """Compare ``actual`` against a stored snapshot.

    When ``SMM_UPDATE_SNAPSHOTS`` is set the snapshot is (re)written from
    ``actual`` instead of asserted, so regenerating after an intentional change
    is a one-liner rather than a hand-edit of the pipe-delimited files.
    """
    if UPDATE_SNAPSHOTS:
        path = SNAPSHOTS / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(actual) + "\n", encoding="utf-8")
        return
    assert actual == load_snapshot(name)


def make_context(config: dict[str, Any] | None = None) -> SyncContext:
    merged_config = DEFAULT_CONFIG.copy()
    if config:
        merged_config.update(config)
    ctx = SyncContext(config=Config.from_dict(merged_config))
    ctx.wstoken = "fake-webservice-token"
    ctx.user_id = 10001
    ctx.session_key = "fake-sesskey"
    return ctx


def node_path(ctx: SyncContext, node: Node) -> Path:
    return get_sanitized_node_path(node, Path(ctx.config.sync_directory))


def download_file(ctx: SyncContext, node: Node) -> bool:
    return downloader.download_file(ctx, node)


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
