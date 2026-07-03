from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from syncmymoodle.node import Node


@dataclass
class SyncContext:
    config: dict[str, Any]
    session: Any = None
    session_key: str | None = None
    wstoken: str | None = None
    user_id: Any = None
    user_private_access_key: str | None = None
    root_node: Node | None = None
    course_caches: dict[Path, Node] = field(default_factory=dict)
    opencast_error_count: int = 0
    opencast_status_hint_logged: bool = False
    sciebo_link_cache: dict[str, Node] = field(default_factory=dict)
    opencast_episode_auth_cache: set[tuple[Any, str]] = field(default_factory=set)
    opencast_track_cache: dict[str, str] = field(default_factory=dict)
    downloaded_paths: set[Path] | None = None
