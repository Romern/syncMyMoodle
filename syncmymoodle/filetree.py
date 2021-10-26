import base64
import hashlib
from pathlib import Path
from typing import Iterable, List, Optional, overload

INVALID_CHARS = frozenset('~"#%&*:<>?/\\{|}')


# TODO split into class for directory and file to improve type checking
# e.g. self.url or ""
class Node:
    def __init__(
        self,
        name: str,
        id,
        type,
        url: str = None,
        is_downloaded: bool = False,
    ):
        self.name = name
        self.id = id
        self.url = url
        self.type = type
        self.children: List[Node] = []
        self.is_downloaded = (
            is_downloaded  # Can also be used to exclude files from being downloaded
        )

    def __repr__(self):
        return f"Node(name={self.name}, id={self.id}, url={self.url}, type={self.type})"

    @property
    def sanitized_name(self) -> str:
        name = "".join(s for s in self.name if s not in INVALID_CHARS)
        return name.strip()

    def list_files(self, root: Path = None) -> Iterable[Path]:
        if not root:
            root = Path("/")
        yield root / self.sanitized_name
        for child in self.children:
            yield from child.list_files(root / self.sanitized_name)

    @overload
    def add_child(self, name: str, id, type: str, url: None = None) -> "Node":
        ...

    @overload
    def add_child(self, name: str, id, type: str, url: str) -> Optional["Node"]:
        ...

    def add_child(self, name: str, id, type: str, url: str = None) -> Optional["Node"]:
        if url:
            url = url.replace("?forcedownload=1", "")
            url = url.replace("webservice/pluginfile.php", "pluginfile.php")

        # Check for duplicate urls and just ignore those nodes:
        if url and any(c.url == url for c in self.children):
            return None

        temp = Node(name, id, type, url=url)
        self.children.append(temp)
        return temp

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
                    child.name = f"{tmp_name}_{(child.url or '').split('/')[-1]}"
                    for s in siblings:
                        tmp_name = Path(s.name).name
                        s.name = f"{s.name}_{(s.url or '').split('/')[-1]}"
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
                c for c in self.children if c.name == child.name and c.url != child.url
            ]
            if len(siblings) > 0:
                # if a filename is still duplicate in its directory, we rename it by appending its id (urlsafe base64 so it also works for urls).
                filename = Path(child.name)
                child.name = (
                    filename.stem
                    + "_"
                    + base64.urlsafe_b64encode(
                        hashlib.md5(str(child.id).encode("utf-8"))
                        .hexdigest()
                        .encode("utf-8")
                    ).decode()[:10]
                    + filename.suffix
                )
                for s in siblings:
                    filename = Path(s.name)
                    s.name = (
                        filename.stem
                        + "_"
                        + base64.urlsafe_b64encode(
                            hashlib.md5(str(s.id).encode("utf-8"))
                            .hexdigest()
                            .encode("utf-8")
                        ).decode()[:10]
                        + filename.suffix
                    )
                    self.children.remove(s)
                unclashed_children.extend(siblings)

        self.children = unclashed_children

        for child in self.children:
            # recurse whole tree
            child.remove_children_nameclashes()
