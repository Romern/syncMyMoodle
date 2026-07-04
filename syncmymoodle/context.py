from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from syncmymoodle.config import Config
from syncmymoodle.node import Node

if TYPE_CHECKING:
    from syncmymoodle.opencast import OpencastTrack


@dataclass
class SyncContext:
    config: Config
    session: requests.Session | None = None
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
    opencast_track_cache: dict[str, OpencastTrack] = field(default_factory=dict)
    downloaded_paths: set[Path] = field(default_factory=set)

    def require_session(self) -> requests.Session:
        """Return the active session, or raise if login() has not run yet."""
        if self.session is None:
            raise Exception("You need to login() first.")
        return self.session
