from __future__ import annotations

import base64
import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Any

from syncmymoodle.pathing import sanitize_path_part, sanitized_node_path_parts

NAME_CLASH_ID_UNSET = object()


class RemoteMarkerKind(StrEnum):
    CONTENT_HASH = "content_hash"
    OPAQUE = "opaque"


class DownloadStatus(StrEnum):
    PENDING = "pending"
    HANDLED = "handled"


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


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


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
        remote_size: Any = None,
        name_clash_id: Any = NAME_CLASH_ID_UNSET,
        download_status: DownloadStatus | str | None = None,
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
        self.remote_size = _optional_int(remote_size)
        self.name_clash_id = (
            id if name_clash_id is NAME_CLASH_ID_UNSET else name_clash_id
        )
        self.download_status = (
            _download_status(download_status) or DownloadStatus.PENDING
        )

    def __repr__(self) -> str:
        return f"Node(name={self.name}, id={self.id}, url={self.url}, type={self.type})"

    @property
    def is_handled(self) -> bool:
        return self.download_status == DownloadStatus.HANDLED

    def mark_handled(self) -> None:
        self.download_status = DownloadStatus.HANDLED

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
    ) -> Node | None:
        if url:
            url = url.replace("?forcedownload=1", "").replace(
                "mod_page/content/3/", "mod_page/content/"
            )
            url = url.replace("webservice/pluginfile.php", "pluginfile.php")

        # Check for duplicate urls and just ignore those nodes:
        if url and any([True for c in self.children if c.url == url]):
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
            remote_size=self.remote_size,
            name_clash_id=self.name_clash_id,
            download_status=self.download_status,
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

    def _clash_suffix(self) -> str:
        # Stable, distinct suffix used to disambiguate same-named siblings.
        # A URL identifies the actual downloadable file, including direct-link
        # nodes whose name_clash_id is None. Non-downloadable nodes such as
        # courses fall back to their stable Moodle id.
        key = self.url if self.url is not None else self.name_clash_id
        return base64.urlsafe_b64encode(
            hashlib.md5(str(key).encode("utf-8")).hexdigest().encode("utf-8")
        ).decode()[:10]

    def _stable_clash_name(self) -> str:
        filename = Path(self.name)
        return filename.stem + "_" + self._clash_suffix() + filename.suffix

    def _opencast_clash_name(self) -> str:
        return f"{Path(self.name).name}_{str(self.url).split('/')[-1]}"

    @staticmethod
    def _filesystem_name_key(node: Node) -> str:
        return sanitize_path_part(node.name).casefold()

    def _filesystem_path_key(self) -> tuple[str, ...]:
        return tuple(part.casefold() for part in sanitized_node_path_parts(self))

    @classmethod
    def _general_name_clash(cls, left: Node, right: Node) -> bool:
        if cls._filesystem_name_key(left) != cls._filesystem_name_key(right):
            return False
        if left.url != right.url:
            return True
        return (
            left.type == "Course"
            and right.type == "Course"
            and left.name_clash_id != right.name_clash_id
        )

    @classmethod
    def _apply_opencast_name_clashes(cls, children: list[Node]) -> list[Node]:
        remaining = children.copy()
        renamed: list[Node] = []

        while remaining:
            child = remaining.pop(0)
            renamed.append(child)
            if child.type != "Opencast":
                continue

            siblings = [
                sibling
                for sibling in remaining
                if cls._filesystem_name_key(sibling) == cls._filesystem_name_key(child)
                and sibling.url != child.url
            ]
            if not siblings:
                continue

            child.name = child._opencast_clash_name()
            for sibling in siblings:
                sibling.name = sibling._opencast_clash_name()
                remaining.remove(sibling)
                renamed.append(sibling)

        return renamed

    @classmethod
    def _apply_general_name_clashes(cls, children: list[Node]) -> list[Node]:
        remaining = children.copy()
        renamed: list[Node] = []

        while remaining:
            child = remaining.pop(0)
            renamed.append(child)
            siblings = [
                sibling
                for sibling in remaining
                if cls._general_name_clash(child, sibling)
            ]
            if not siblings:
                continue

            child.name = child._stable_clash_name()
            for sibling in siblings:
                sibling.name = sibling._stable_clash_name()
                remaining.remove(sibling)
                renamed.append(sibling)

        return renamed

    def _remove_sibling_nameclashes(self) -> None:
        self.children = self._apply_opencast_name_clashes(self.children)
        self.children = self._apply_general_name_clashes(self.children)

        for child in self.children:
            child._remove_sibling_nameclashes()

    def _resolve_download_path_clashes(self) -> None:
        download_nodes: list[Node] = []
        remaining = [self]
        while remaining:
            node = remaining.pop()
            remaining.extend(node.children)
            if node.url:
                download_nodes.append(node)

        # Each pass either resolves every collision or moves a colliding file
        # away from a pre-existing generated name. At most one such name can be
        # consumed per file; the bound prevents a malformed tree from looping.
        for _ in range(len(download_nodes) + 1):
            nodes_by_path: dict[tuple[str, ...], list[Node]] = {}
            for node in download_nodes:
                nodes_by_path.setdefault(node._filesystem_path_key(), []).append(node)
            clashes = [
                nodes
                for nodes in nodes_by_path.values()
                if len({node.url for node in nodes}) > 1
            ]
            if not clashes:
                return
            for nodes in clashes:
                for node in nodes:
                    node.name = node._stable_clash_name()
        raise ValueError("Could not create unique paths for downloaded files")

    def remove_children_nameclashes(self) -> None:
        self._remove_sibling_nameclashes()
        self._resolve_download_path_clashes()
