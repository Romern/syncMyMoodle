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

    def __repr__(self) -> str:
        return (
            f"Node(name={self.name}, id={self.id}, url={self.url}, type={self.type}, "
            f"download_kind={self.download_kind})"
        )

    @property
    def is_handled(self) -> bool:
        return self.download_status != DownloadStatus.PENDING

    @property
    def is_verified(self) -> bool:
        return self.download_status == DownloadStatus.HANDLED

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
    ) -> Node | None:
        # Check for duplicate urls and just ignore those nodes:
        if url and any(child.url == url for child in self.children):
            return None

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
        return clone

    def get_path(self) -> list[str]:
        ret: list[str] = []
        cur: Node | None = self
        while cur is not None:
            ret.insert(0, cur.name)
            cur = cur.parent
        return ret
