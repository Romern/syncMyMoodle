from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any, cast

NAME_CLASH_ID_UNSET = object()


class Node:
    def __init__(
        self,
        name: str,
        id: Any,
        type: str,  # noqa: A003 - keep original name for compatibility
        parent: Node | None,
        url: str | None = None,
        additional_info: Any = None,
        timemodified: Any = None,
        etag: str | None = None,
        name_clash_id: Any = NAME_CLASH_ID_UNSET,
        is_downloaded: bool = False,
    ) -> None:
        self.name = name
        self.id = id
        self.url = url
        self.type = type
        self.parent = parent
        self.children: list[Node] = []
        # Currently only used for course_id in opencast, auth header in sciebo,
        # and may be extended for other module-specific data.
        self.additional_info = additional_info
        self.timemodified = timemodified
        self.etag = etag
        self.name_clash_id = (
            id if name_clash_id is NAME_CLASH_ID_UNSET else name_clash_id
        )
        self.is_downloaded = (
            is_downloaded  # Can also be used to exclude files from being downloaded
        )

    def __repr__(self) -> str:
        return f"Node(name={self.name}, id={self.id}, url={self.url}, type={self.type})"

    def add_child(
        self,
        name: str,
        id: Any,
        type: str,  # noqa: A003 - keep original name for compatibility
        url: str | None = None,
        additional_info: Any = None,
        timemodified: Any = None,
        etag: str | None = None,
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
            additional_info=additional_info,
            timemodified=timemodified,
            etag=etag,
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
            additional_info=self.additional_info,
            timemodified=self.timemodified,
            etag=self.etag,
            name_clash_id=self.name_clash_id,
            is_downloaded=self.is_downloaded,
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

    def go_to_path(self, target_path: list[str]) -> Node:
        target_node = [self]
        for path_child in target_path:
            if path_child == "":
                continue
            try:
                target_node.append(
                    [
                        node_child
                        for node_child in target_node[-1].children
                        if node_child.name == path_child
                    ][0]
                )
            except IndexError:
                raise Exception("The path is not found in this root node. Wrong path?")
        return target_node[-1]

    def _clash_suffix(self) -> str:
        # Stable, distinct suffix used to disambiguate same-named siblings.
        # Fall back to the URL when no name_clash_id is set (direct-link,
        # embedded, and direct-content file nodes pass name_clash_id=None);
        # otherwise such nodes would all hash to md5("None") and collide onto
        # the same path, silently dropping all but one file.
        key = self.name_clash_id if self.name_clash_id is not None else self.url
        return base64.urlsafe_b64encode(
            hashlib.md5(str(key).encode("utf-8")).hexdigest().encode("utf-8")
        ).decode()[:10]

    def remove_children_nameclashes(self) -> None:
        # Check for duplicate filenames

        unclashed_children = []
        # work on copy since deleting from the iterated list breaks stuff
        copy_children = self.children.copy()
        for child in copy_children:
            if child not in self.children:
                continue
            self.children.remove(child)
            unclashed_children.append(child)
            if child.type == "Opencast":
                siblings = [
                    c
                    for c in self.children
                    if c.name == child.name and c.url != child.url
                ]
                if len(siblings) > 0:
                    # if an Opencast filename is duplicate in its directory, we append the filename as it was uploaded
                    tmp_name = Path(child.name).name
                    child.name = f"{tmp_name}_{cast(str, child.url).split('/')[-1]}"
                    for s in siblings:
                        tmp_name = Path(s.name).name
                        s.name = f"{s.name}_{cast(str, s.url).split('/')[-1]}"
                        self.children.remove(s)
                    unclashed_children.extend(siblings)

        self.children = unclashed_children

        unclashed_children = []
        copy_children = self.children.copy()
        for child in copy_children:
            if child not in self.children:
                continue
            self.children.remove(child)
            unclashed_children.append(child)
            siblings = [
                c
                for c in self.children
                if c.name == child.name
                and (
                    c.url != child.url
                    # Course prefix handling may create duplicate URL-less course
                    # folders. Other URL-less nodes, such as duplicate Moodle
                    # sections, keep the legacy behavior and merge silently.
                    or (
                        child.type == "Course"
                        and c.type == "Course"
                        and c.name_clash_id != child.name_clash_id
                    )
                )
            ]
            if len(siblings) > 0:
                # if a filename is still duplicate in its directory, we rename
                # it by appending a stable per-node key (works for ids and urls).
                filename = Path(child.name)
                child.name = (
                    filename.stem + "_" + child._clash_suffix() + filename.suffix
                )
                for s in siblings:
                    filename = Path(s.name)
                    s.name = filename.stem + "_" + s._clash_suffix() + filename.suffix
                    self.children.remove(s)
                unclashed_children.extend(siblings)

        self.children = unclashed_children

        for child in self.children:
            # recurse whole tree
            child.remove_children_nameclashes()
