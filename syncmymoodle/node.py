from __future__ import annotations

from enum import StrEnum
from typing import Any

NAME_CLASH_ID_UNSET = object()


class RemoteMarkerKind(StrEnum):
    CONTENT_HASH = "content_hash"
    OPAQUE = "opaque"


class DownloadStatus(StrEnum):
    PENDING = "pending"
    HANDLED = "handled"
    SKIPPED = "skipped"


class DownloadKind(StrEnum):
    """Download behavior recorded separately from the display-only node type."""

    DIRECT = "direct"
    YOUTUBE = "youtube"
    EMEDIA = "emedia"
    QUIZ = "quiz"
    OPENCAST = "opencast"


class NodeKind(StrEnum):
    """Structural roles in the synchronized Moodle tree."""

    ROOT = "Root"
    SEMESTER = "Semester"
    COURSE = "Course"
    SECTION = "Section"


def _remote_marker_kind(
    value: RemoteMarkerKind | str | None,
) -> RemoteMarkerKind | None:
    if value is None:
        return None
    try:
        return RemoteMarkerKind(value)
    except ValueError:
        return None


def _download_status(value: DownloadStatus | str | None) -> DownloadStatus | None:
    if value is None:
        return None
    try:
        return DownloadStatus(value)
    except ValueError:
        return None


def _download_kind(value: DownloadKind | str | None) -> DownloadKind:
    try:
        return DownloadKind(value or DownloadKind.DIRECT)
    except ValueError:
        return DownloadKind.DIRECT


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _artifact_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: digest
        for key, digest in value.items()
        if isinstance(key, str)
        and key.isalnum()
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    }


class Node:
    def __init__(
        self,
        name: str,
        id: Any,
        type: str,  # noqa: A003 - keep original name for compatibility
        parent: Node | None,
        url: str | None = None,
        download_headers: dict[str, str] | None = None,
        timemodified: Any = None,
        etag: str | None = None,
        etag_kind: RemoteMarkerKind | str | None = None,
        content_hash: str | None = None,
        artifact_hashes: dict[str, str] | None = None,
        remote_size: Any = None,
        name_clash_id: Any = NAME_CLASH_ID_UNSET,
        download_status: DownloadStatus | str | None = None,
        download_kind: DownloadKind | str | None = None,
    ) -> None:
        self.name = name
        self.id = id
        self.url = url
        self.type = type
        self.parent = parent
        self.children: list[Node] = []
        self.download_headers = dict(download_headers) if download_headers else None
        self.timemodified = timemodified
        self.etag = etag
        self.etag_kind = _remote_marker_kind(etag_kind)
        # A content hash (sha256 hex) we compute from the bytes we downloaded.
        # Unlike etag, which for Sciebo/WebDAV is an opaque revision token, this
        # is a real hash of our copy, used to detect local user modifications.
        self.content_hash = content_hash
        self.artifact_hashes = _artifact_hashes(artifact_hashes)
        self.remote_size = _optional_int(remote_size)
        self.name_clash_id = (
            id if name_clash_id is NAME_CLASH_ID_UNSET else name_clash_id
        )
        self.download_status = (
            _download_status(download_status) or DownloadStatus.PENDING
        )
        self.download_kind = _download_kind(download_kind)
        self._conflicting_download_metadata: set[str] = set()

    def __repr__(self) -> str:
        return (
            f"Node(has_url={self.url is not None}, type={self.type}, "
            f"download_kind={self.download_kind})"
        )

    @property
    def is_handled(self) -> bool:
        return self.download_status != DownloadStatus.PENDING

    @property
    def is_verified(self) -> bool:
        return self.download_status == DownloadStatus.HANDLED

    @property
    def has_remote_marker_conflict(self) -> bool:
        return "remote_marker" in self._conflicting_download_metadata

    def mark_handled(self) -> None:
        self.download_status = DownloadStatus.HANDLED

    def mark_skipped(self) -> None:
        self.download_status = DownloadStatus.SKIPPED

    def add_child(
        self,
        name: str,
        id: Any,
        type: str,  # noqa: A003 - keep original name for compatibility
        url: str | None = None,
        download_headers: dict[str, str] | None = None,
        timemodified: Any = None,
        etag: str | None = None,
        etag_kind: RemoteMarkerKind | str | None = None,
        remote_size: Any = None,
        name_clash_id: Any = NAME_CLASH_ID_UNSET,
        download_kind: DownloadKind | str | None = None,
    ) -> Node:
        temp = Node(
            name,
            id,
            type,
            self,
            url=url,
            download_headers=download_headers,
            timemodified=timemodified,
            etag=etag,
            etag_kind=etag_kind,
            remote_size=remote_size,
            name_clash_id=name_clash_id,
            download_kind=download_kind,
        )
        self.children.append(temp)
        return temp

    @staticmethod
    def _reconcile_download_metadata(existing: Node, candidate: Node) -> None:
        for attr in ("download_headers", "timemodified", "remote_size"):
            if attr in existing._conflicting_download_metadata:
                continue
            old = getattr(existing, attr)
            new = getattr(candidate, attr)
            if old is None:
                setattr(existing, attr, new)
            elif new is not None and old != new:
                setattr(existing, attr, None)
                existing._conflicting_download_metadata.add(attr)

        if "remote_marker" in existing._conflicting_download_metadata:
            return
        if existing.etag is None and candidate.etag is not None:
            existing.etag = candidate.etag
            existing.etag_kind = candidate.etag_kind
        elif candidate.etag is not None and (
            existing.etag != candidate.etag
            or (
                existing.etag_kind is not None
                and candidate.etag_kind is not None
                and existing.etag_kind != candidate.etag_kind
            )
        ):
            existing.etag = None
            existing.etag_kind = None
            existing._conflicting_download_metadata.add("remote_marker")
        elif candidate.etag is not None and existing.etag_kind is None:
            existing.etag_kind = candidate.etag_kind

    def add_download_child(
        self,
        name: str,
        id: Any,
        type: str,  # noqa: A003 - keep original name for compatibility
        *,
        url: str,
        download_headers: dict[str, str] | None = None,
        timemodified: Any = None,
        etag: str | None = None,
        etag_kind: RemoteMarkerKind | str | None = None,
        remote_size: Any = None,
        name_clash_id: Any = NAME_CLASH_ID_UNSET,
        download_kind: DownloadKind | str | None = None,
    ) -> Node:
        """Add one discovered download, reconciling a compatible URL duplicate.

        Structural insertion remains unconditional in :meth:`add_child`. This
        operation makes provider-level materialization deduplication explicit:
        a repeated name and URL strengthens missing metadata instead of
        creating two downloads for the same target path.
        """
        candidate = Node(
            name,
            id,
            type,
            self,
            url=url,
            download_headers=download_headers,
            timemodified=timemodified,
            etag=etag,
            etag_kind=etag_kind,
            remote_size=remote_size,
            name_clash_id=name_clash_id,
            download_kind=download_kind,
        )
        existing = next(
            (
                child
                for child in self.children
                if child.url == url and child.name == candidate.name
            ),
            None,
        )
        if existing is None:
            self.children.append(candidate)
            return candidate
        if (
            existing.type != candidate.type
            or existing.download_kind is not candidate.download_kind
        ):
            raise ValueError(
                "conflicting download semantics for the same target name and URL"
            )
        self._reconcile_download_metadata(existing, candidate)
        return existing

    def ancestor(self, kind: NodeKind) -> Node | None:
        """Return this node or its nearest ancestor with the structural kind."""
        current: Node | None = self
        while current is not None:
            if current.type == kind:
                return current
            current = current.parent
        return None

    def clone(self, parent: Node | None = None) -> Node:
        clone = Node(
            self.name,
            self.id,
            self.type,
            parent,
            url=self.url,
            download_headers=self.download_headers,
            timemodified=self.timemodified,
            etag=self.etag,
            etag_kind=self.etag_kind,
            content_hash=self.content_hash,
            artifact_hashes=self.artifact_hashes,
            remote_size=self.remote_size,
            name_clash_id=self.name_clash_id,
            download_status=self.download_status,
            download_kind=self.download_kind,
        )
        clone.children = [child.clone(clone) for child in self.children]
        clone._conflicting_download_metadata = set(self._conflicting_download_metadata)
        return clone

    def get_path(self) -> list[str]:
        ret: list[str] = []
        cur: Node | None = self
        while cur is not None:
            ret.insert(0, cur.name)
            cur = cur.parent
        return ret


def match_equivalent_child(parent: Node | None, child: Node) -> Node | None:
    """Find the structurally equivalent child below ``parent``, if any."""
    if parent is None:
        return None
    candidates = [
        candidate
        for candidate in parent.children
        if candidate.name == child.name and candidate.type == child.type
    ]
    if not candidates:
        return None

    for attr in ("url", "name_clash_id", "id"):
        child_value = getattr(child, attr)
        if child_value is None:
            continue
        for candidate in candidates:
            if getattr(candidate, attr) == child_value:
                return candidate
    return candidates[0]
