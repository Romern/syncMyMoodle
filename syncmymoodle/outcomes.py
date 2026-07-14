"""Typed download outcomes and statistics for one sync run."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class DownloadState(Enum):
    """Whether a download node may be persisted as handled."""

    HANDLED = "handled"
    FAILED = "failed"


@dataclass(frozen=True)
class DownloadOutcome:
    """The complete user-visible result of processing one download node."""

    state: DownloadState = DownloadState.HANDLED
    downloaded: int = 0
    updated: int = 0
    unchanged: int = 0
    planned: int = 0
    transferred_bytes: int = 0

    @property
    def is_handled(self) -> bool:
        return self.state is DownloadState.HANDLED

    def __bool__(self) -> bool:
        raise TypeError("Use DownloadOutcome.is_handled instead of truth testing")

    def merge(self, other: DownloadOutcome) -> DownloadOutcome:
        """Combine artifact outcomes belonging to the same download node."""
        state = (
            DownloadState.FAILED
            if DownloadState.FAILED in (self.state, other.state)
            else DownloadState.HANDLED
        )
        return DownloadOutcome(
            state=state,
            downloaded=self.downloaded + other.downloaded,
            updated=self.updated + other.updated,
            unchanged=self.unchanged + other.unchanged,
            planned=self.planned + other.planned,
            transferred_bytes=self.transferred_bytes + other.transferred_bytes,
        )


HANDLED_DOWNLOAD = DownloadOutcome()
FAILED_DOWNLOAD = DownloadOutcome(state=DownloadState.FAILED)
UNCHANGED_DOWNLOAD = DownloadOutcome(unchanged=1)
PLANNED_DOWNLOAD = DownloadOutcome(planned=1)


def completed_download(*, existed: bool, transferred_bytes: int = 0) -> DownloadOutcome:
    """Build the outcome of installing one requested artifact."""
    return DownloadOutcome(
        downloaded=int(not existed),
        updated=int(existed),
        transferred_bytes=max(0, transferred_bytes),
    )


@dataclass
class RunStatistics:
    """User-relevant outcomes accumulated during one sync run."""

    courses: int = 0
    downloaded: int = 0
    updated: int = 0
    unchanged: int = 0
    planned: int = 0
    failed: int = 0
    transferred_bytes: int = 0
    started_at: float = field(default_factory=time.monotonic, repr=False)

    def record_download(self, outcome: DownloadOutcome) -> None:
        self.downloaded += outcome.downloaded
        self.updated += outcome.updated
        self.unchanged += outcome.unchanged
        self.planned += outcome.planned
        self.transferred_bytes += outcome.transferred_bytes
        self.failed += int(not outcome.is_handled)

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)
