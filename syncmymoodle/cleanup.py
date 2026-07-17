"""Local cleanup helpers for sync artifacts."""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from syncmymoodle.constants import COURSE_CACHE_DIRECTORY, COURSE_CACHE_FILENAME
from syncmymoodle.pathing import CONFLICT_GLOB, InternalPathRoot, parse_conflict_path


@dataclass(frozen=True)
class ConflictFile:
    path: Path
    canonical: Path
    content_hash: str


@dataclass(frozen=True)
class ConflictCleanupPlan:
    remove: tuple[Path, ...]
    keep: tuple[ConflictFile, ...]


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def iter_conflicts(root: Path | InternalPathRoot) -> list[ConflictFile]:
    internal_root = InternalPathRoot.resolve(root)
    conflicts: list[ConflictFile] = []
    for discovered_path in internal_root.root.rglob(CONFLICT_GLOB):
        path = internal_root.require(discovered_path)
        if not path.is_file():
            continue
        conflict_path = parse_conflict_path(path)
        if conflict_path is None:
            continue
        canonical = internal_root.require(conflict_path.canonical)
        conflicts.append(
            ConflictFile(
                path=path,
                canonical=canonical,
                content_hash=file_hash(path),
            )
        )
    return conflicts


def duplicate_keep_key(path: Path) -> tuple[int, float, str]:
    conflict_path = parse_conflict_path(path)
    index = conflict_path.index if conflict_path else 0
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    return index, mtime, os.fspath(path)


def conflict_cleanup_plan(conflicts: Iterable[ConflictFile]) -> ConflictCleanupPlan:
    remove: set[Path] = set()
    keep: list[ConflictFile] = []
    by_canonical: dict[Path, list[ConflictFile]] = defaultdict(list)
    for conflict in conflicts:
        by_canonical[conflict.canonical].append(conflict)

    for canonical, group in by_canonical.items():
        current_hash = file_hash(canonical) if canonical.is_file() else None
        remaining: list[ConflictFile] = []

        for conflict in group:
            if current_hash is not None and conflict.content_hash == current_hash:
                remove.add(conflict.path)
            else:
                remaining.append(conflict)

        by_hash: dict[str, list[ConflictFile]] = defaultdict(list)
        for conflict in remaining:
            by_hash[conflict.content_hash].append(conflict)

        for duplicate_group in by_hash.values():
            ordered = sorted(
                duplicate_group,
                key=lambda item: duplicate_keep_key(item.path),
            )
            keep.append(ordered[0])
            remove.update(duplicate.path for duplicate in ordered[1:])

    return ConflictCleanupPlan(tuple(sorted(remove)), tuple(keep))


def iter_course_caches(root: Path | InternalPathRoot) -> list[Path]:
    internal_root = InternalPathRoot.resolve(root)
    internal_root.path(COURSE_CACHE_DIRECTORY)
    caches = []
    for discovered_path in internal_root.root.rglob(COURSE_CACHE_FILENAME):
        path = internal_root.require(discovered_path)
        if path.is_file():
            caches.append(path)
    return sorted(caches)


def delete_paths(root: Path | InternalPathRoot, paths: Iterable[Path]) -> None:
    internal_root = InternalPathRoot.resolve(root)
    for path in paths:
        internal_root.require(path).unlink()
