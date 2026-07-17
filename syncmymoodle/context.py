from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from syncmymoodle.config import Config
from syncmymoodle.http_utils import ServiceOutageTracker
from syncmymoodle.moodle_tokens import MoodleTokens
from syncmymoodle.node import Node
from syncmymoodle.outcomes import RunStatistics
from syncmymoodle.output import TerminalOutput, get_output
from syncmymoodle.pathing import InternalPathRoot

if TYPE_CHECKING:
    from syncmymoodle.course_cache import CourseCacheState
    from syncmymoodle.emedia import EmediaResolution
    from syncmymoodle.links import LinkedResourceResolution
    from syncmymoodle.opencast import (
        OpencastEpisode,
        OpencastMetadataState,
    )


# Retain a small overlap so a change committed at the integer-second token
# validation boundary cannot fall between incremental update queries.
MOODLE_UPDATE_OVERLAP_SECONDS = 5
ModuleInstanceCache = dict[int, dict[int, dict[str, Any]] | None]


class BrowserSessionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class MoodleAccount:
    """Validated Moodle tokens paired with their Moodle user id."""

    tokens: MoodleTokens = field(repr=False)

    @property
    def wstoken(self) -> str:
        return self.tokens.wstoken

    @property
    def user_id(self) -> int:
        assert self.tokens.moodle_user_id is not None
        return self.tokens.moodle_user_id


@dataclass
class AuthState:
    """Mutable credentials and deferred resolvers for one login attempt."""

    user: str | None = None
    password: str | None = field(default=None, repr=False)
    totp_serial: str | None = None
    totp_secret: str | None = field(default=None, repr=False)
    otp_code: str | None = field(default=None, repr=False)
    credential_resolver: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    otp_code_resolver: Callable[[], str | None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_config(cls, config: Config) -> AuthState:
        return cls(
            user=config.user,
            totp_serial=config.totp_serial,
        )


@dataclass(frozen=True, order=True)
class FilteredItem:
    """One item deliberately excluded by the configured sync policy."""

    config_key: str
    category: str
    item: str
    reason: str


@dataclass(frozen=True)
class LinkedResourceCacheEntry:
    """Revalidation metadata and optional HTML for one followed link."""

    final_url: str
    content_type: str
    html: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    fresh_until: float | None = None
    remote_size: int | None = None


@dataclass
class SyncContext:
    config: Config
    output: TerminalOutput = field(
        default_factory=get_output, repr=False, compare=False
    )
    stats: RunStatistics = field(
        default_factory=RunStatistics, repr=False, compare=False
    )
    auth: AuthState = field(init=False)
    internal_path_root: InternalPathRoot = field(init=False, repr=False, compare=False)
    session: requests.Session | None = None
    session_key: str | None = field(default=None, repr=False)
    moodle_account: MoodleAccount | None = field(default=None, repr=False)
    browser_session: requests.Session | None = field(default=None, repr=False)
    browser_session_key: str | None = field(default=None, repr=False)
    browser_session_resolver: Callable[[], None] | None = field(
        default=None, repr=False, compare=False
    )
    emedia_api_session: requests.Session | None = field(default=None, repr=False)
    root_node: Node | None = None
    course_cache_states: dict[Node, CourseCacheState] = field(
        default_factory=dict,
        repr=False,
    )
    moodle_functions: frozenset[str] = field(default_factory=frozenset, repr=False)
    moodle_server_time: int | None = field(default=None, repr=False)
    browser_bootstrap_error_logged: bool = False
    # None negatively caches a share that already failed during this run.
    sciebo_link_cache: dict[str, Node | None] = field(default_factory=dict)
    service_outages: ServiceOutageTracker = field(default_factory=ServiceOutageTracker)
    opencast_course_auth_cache: set[tuple[str, str]] = field(default_factory=set)
    opencast_episode_cache: dict[tuple[str | None, str], OpencastEpisode] = field(
        default_factory=dict
    )
    opencast_seen_episodes: set[tuple[str, str]] = field(default_factory=set)
    opencast_metadata_states: dict[tuple[str | None, str], OpencastMetadataState] = (
        field(default_factory=dict)
    )
    opencast_series_cache: dict[
        tuple[str | None, str], tuple[tuple[str, str], ...] | None
    ] = field(default_factory=dict)
    emedia_video_cache: dict[int, EmediaResolution] = field(default_factory=dict)
    emedia_revision_cache: dict[str, str | None] = field(default_factory=dict)
    emedia_output_suffix: str | None = None
    downloaded_paths: set[Path] = field(default_factory=set)
    filtered_items: set[FilteredItem] = field(default_factory=set)
    quiz_review_cache: dict[str, str] = field(default_factory=dict)
    lti_instance_cache: ModuleInstanceCache = field(default_factory=dict)
    h5p_activity_cache: ModuleInstanceCache = field(default_factory=dict)
    quiz_instance_cache: ModuleInstanceCache = field(default_factory=dict)
    linked_resources_by_course: dict[str, dict[str, LinkedResourceCacheEntry]] = field(
        default_factory=dict
    )
    linked_resource_results: dict[str, LinkedResourceResolution] = field(
        default_factory=dict
    )
    seen_linked_resources: set[tuple[str, str]] = field(default_factory=set)
    incomplete_course_ids: set[int] = field(default_factory=set)
    reported_course_failure_sources: set[tuple[int, str]] = field(
        default_factory=set,
        repr=False,
    )
    legacy_course_cache_paths: dict[int, list[Path]] | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.auth = AuthState.from_config(self.config)
        self.internal_path_root = InternalPathRoot.resolve(
            Path(self.config.sync_directory)
        )

    @property
    def moodle_update_watermark(self) -> int | None:
        """Timestamp used for incremental queries, including a safe overlap."""
        if self.moodle_server_time is None:
            return None
        return max(0, self.moodle_server_time - MOODLE_UPDATE_OVERLAP_SECONDS)

    def record_filtered(
        self,
        config_key: str,
        category: str,
        item: str,
        reason: str,
    ) -> None:
        self.filtered_items.add(FilteredItem(config_key, category, item, reason))

    def mark_course_incomplete(self, course_id: Any) -> None:
        """Prevent a partial course inventory from replacing its previous cache."""
        if (
            isinstance(course_id, int)
            and not isinstance(course_id, bool)
            and course_id > 0
        ):
            self.incomplete_course_ids.add(course_id)

    def record_course_failure(self, course_id: Any) -> None:
        """Record a failed course source and retain its last complete cache."""
        self.stats.failed += 1
        self.mark_course_incomplete(course_id)

    def record_course_failure_once(self, course_id: Any, source: str) -> None:
        """Record one failed source once per affected course."""
        if (
            isinstance(course_id, int)
            and not isinstance(course_id, bool)
            and course_id > 0
        ):
            failure_key = (course_id, source)
            if failure_key in self.reported_course_failure_sources:
                return
            self.reported_course_failure_sources.add(failure_key)
        self.record_course_failure(course_id)

    def require_session(self) -> requests.Session:
        """Return the token-capable general HTTP session."""
        if self.session is None:
            raise Exception("Authentication has not been initialized.")
        return self.session

    def require_moodle_account(self) -> MoodleAccount:
        if self.moodle_account is None:
            raise Exception("Moodle account authentication has not been initialized.")
        return self.moodle_account

    def require_browser_session(self) -> requests.Session:
        if self.browser_session is None and self.browser_session_resolver is not None:
            self.browser_session_resolver()
        if self.browser_session is None:
            raise BrowserSessionUnavailable("Moodle browser session is unavailable")
        return self.browser_session
