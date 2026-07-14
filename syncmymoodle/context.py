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

if TYPE_CHECKING:
    from syncmymoodle.emedia import EmediaVideo
    from syncmymoodle.opencast import OpencastTrack


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
    course_caches: dict[Path, Node] = field(default_factory=dict)
    course_cache_payloads: dict[Path, dict[str, Any] | None] = field(
        default_factory=dict,
        repr=False,
    )
    h5p_content_caches: dict[Path, dict[int, tuple[str, str]]] = field(
        default_factory=dict,
        repr=False,
    )
    browser_bootstrap_error_logged: bool = False
    # None negatively caches a share that already failed during this run.
    sciebo_link_cache: dict[str, Node | None] = field(default_factory=dict)
    service_outages: ServiceOutageTracker = field(default_factory=ServiceOutageTracker)
    opencast_episode_auth_cache: set[tuple[Any, str]] = field(default_factory=set)
    opencast_track_cache: dict[str, tuple[OpencastTrack, ...]] = field(
        default_factory=dict
    )
    emedia_video_cache: dict[int, EmediaVideo | None] = field(default_factory=dict)
    emedia_revision_cache: dict[str, str] = field(default_factory=dict)
    emedia_output_suffix: str | None = None
    downloaded_paths: set[Path] = field(default_factory=set)
    filtered_items: set[FilteredItem] = field(default_factory=set)
    quiz_review_cache: dict[str, str] = field(default_factory=dict)
    lti_instance_cache: dict[int, dict[str, Any]] = field(default_factory=dict)
    h5p_activity_cache: dict[int, dict[str, Any]] = field(default_factory=dict)
    quiz_instance_cache: dict[int, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.auth = AuthState.from_config(self.config)

    def record_filtered(
        self,
        config_key: str,
        category: str,
        item: str,
        reason: str,
    ) -> None:
        self.filtered_items.add(FilteredItem(config_key, category, item, reason))

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
