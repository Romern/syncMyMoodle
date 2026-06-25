import hashlib
import os

from syncmymoodle.__main__ import Node

from .helpers import FakeResponse, FakeSession, build_single_file_tree, make_syncer

URL = (
    "https://moodle.rwth-aachen.de/pluginfile.php/301/mod_resource/content/1/slides.pdf"
)


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def seed_course_cache(config, *, timemodified, etag):
    """Write a per-course cache to disk describing a previously synced file."""
    cache_syncer = make_syncer(config)
    cached_root, _ = build_single_file_tree(
        "slides.pdf", URL, timemodified=timemodified, etag=etag
    )
    cache_syncer.root_node = cached_root
    cache_syncer.cache_root_node()


def make_run_syncer(config, *, timemodified):
    """Return a syncer plus the leaf node for the current (changed) sync run."""
    syncer = make_syncer(config)
    syncer.session = FakeSession()
    _, file_node = build_single_file_tree("slides.pdf", URL, timemodified=timemodified)
    return syncer, file_node


# --------------------------------------------------------------------------
# Actual download happy path (gap 2)
# --------------------------------------------------------------------------


def test_download_streams_chunks_to_disk_and_records_metadata(tmp_path):
    config = {"basedir": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = syncer.get_sanitized_node_path(file_node)
    chunks = [b"%PDF-1.4 first-chunk ", b"second-chunk ", b"third-chunk"]
    etag = '"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"'
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf", "ETag": etag},
            chunks=chunks,
        ),
    )

    assert syncer.download_file(file_node) is True

    assert download_path.read_bytes() == b"".join(chunks)
    # The temporary part-file is renamed away once the download completes.
    assert not download_path.with_suffix(download_path.suffix + ".temp").exists()
    # mtime is aligned with Moodle's timemodified so later runs detect changes.
    assert int(download_path.stat().st_mtime) == 1710000500
    # The ETag is persisted on the node for the next run's change detection.
    assert file_node.etag == etag
    assert syncer.session.count("GET", URL) == 1


def test_download_is_skipped_for_excluded_filetypes(tmp_path):
    config = {"basedir": str(tmp_path), "exclude_filetypes": ["pdf"]}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = syncer.get_sanitized_node_path(file_node)

    # No GET route registered: a request would raise in the fake session.
    assert syncer.download_file(file_node) is True
    assert not download_path.exists()
    assert syncer.session.calls == []


def test_download_path_is_deduplicated_within_a_run(tmp_path):
    # Two distinct nodes that resolve to the same on-disk path must download
    # only once, exercising the lazy-initialised ``_downloaded_paths`` guard.
    config = {"basedir": str(tmp_path)}
    syncer = make_syncer(config)
    syncer.session = FakeSession()
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[b"data"]),
    )
    _, first_node = build_single_file_tree("dup.pdf", URL)
    section = first_node.parent
    # Bypass add_child's duplicate-url guard to get a second node at the same path.
    second_node = Node(
        "dup.pdf", URL + "?v=2", "Linked file [application/pdf]", section, url=URL
    )
    section.children.append(second_node)

    assert syncer.download_file(first_node) is True
    assert syncer.download_file(second_node) is True
    assert syncer.session.count("GET", URL) == 1


# --------------------------------------------------------------------------
# update_files_conflict handling (gap 1)
# --------------------------------------------------------------------------


def _setup_conflict(tmp_path, conflict_mode):
    """Cache a file, then locally modify it so the next run sees a conflict."""
    original = b"original remote content"
    local_modified = b"locally edited content"
    config = {
        "basedir": str(tmp_path),
        "updatefiles": True,
        "update_files_conflict": conflict_mode,
    }
    seed_course_cache(config, timemodified=1710000300, etag=sha1(original))

    syncer, file_node = make_run_syncer(config, timemodified=1710000400)
    download_path = syncer.get_sanitized_node_path(file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(local_modified)
    return syncer, file_node, download_path, local_modified


def _add_new_remote(syncer, body=b"updated remote content"):
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[body]),
    )
    return body


def test_conflict_keep_preserves_local_file_and_skips_download(tmp_path):
    syncer, file_node, download_path, local_modified = _setup_conflict(tmp_path, "keep")

    # No GET registered: keep mode must not contact the server at all.
    assert syncer.download_file(file_node) is True
    assert download_path.read_bytes() == local_modified
    assert syncer.session.calls == []


def test_conflict_none_behaves_like_keep(tmp_path):
    syncer, file_node, download_path, local_modified = _setup_conflict(tmp_path, "none")

    assert syncer.download_file(file_node) is True
    assert download_path.read_bytes() == local_modified
    assert syncer.session.calls == []


def test_conflict_overwrite_replaces_local_file(tmp_path):
    syncer, file_node, download_path, _ = _setup_conflict(tmp_path, "overwrite")
    new_body = _add_new_remote(syncer)

    assert syncer.download_file(file_node) is True
    assert download_path.read_bytes() == new_body
    # Overwrite mode leaves no side-car conflict copy behind.
    assert list(download_path.parent.glob("*.syncconflict.*")) == []
    assert syncer.session.count("GET", URL) == 1


def test_conflict_rename_moves_local_file_aside_before_download(tmp_path):
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "rename"
    )
    new_body = _add_new_remote(syncer)

    assert syncer.download_file(file_node) is True

    # The fresh remote content lands at the canonical path.
    assert download_path.read_bytes() == new_body
    # The user's local edits are preserved in a side-car conflict file.
    conflicts = list(download_path.parent.glob("*.syncconflict.*"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == local_modified
    assert syncer.session.count("GET", URL) == 1


def test_unknown_conflict_mode_defaults_to_rename(tmp_path):
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "bogus-mode"
    )
    new_body = _add_new_remote(syncer)

    assert syncer.download_file(file_node) is True
    assert download_path.read_bytes() == new_body
    conflicts = list(download_path.parent.glob("*.syncconflict.*"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == local_modified


def test_unchanged_timemodified_skips_download_despite_local_edit(tmp_path):
    # When Moodle reports the same timemodified as the cache, the file is
    # considered unchanged remotely and the local copy is left untouched.
    original = b"original remote content"
    config = {
        "basedir": str(tmp_path),
        "updatefiles": True,
        "update_files_conflict": "rename",
    }
    seed_course_cache(config, timemodified=1710000300, etag=sha1(original))
    syncer, file_node = make_run_syncer(config, timemodified=1710000300)
    download_path = syncer.get_sanitized_node_path(file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(b"locally edited content")

    assert syncer.download_file(file_node) is True
    assert syncer.session.calls == []
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def test_etag_failure_falls_back_to_timestamp_heuristic_conflict(tmp_path, monkeypatch):
    # A faulty ETag cache is treated as if there were no cached ETag, so the
    # timestamp heuristic decides. Here the local mtime differs from the cached
    # Moodle timestamp, so it is a conflict and the local edits are kept aside.
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "rename"
    )
    new_body = _add_new_remote(syncer)

    def boom(path, etag):
        raise OSError("cannot read file for hashing")

    monkeypatch.setattr(syncer, "_local_file_matches_etag", boom)

    assert syncer.download_file(file_node) is True
    assert download_path.read_bytes() == new_body
    conflicts = list(download_path.parent.glob("*.syncconflict.*"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == local_modified


def test_etag_failure_falls_back_to_timestamp_heuristic_no_conflict(
    tmp_path, monkeypatch
):
    # Same fallback, but the local mtime matches the cached Moodle timestamp, so
    # the timestamp heuristic reports no local change and the file is updated
    # cleanly without leaving a side-car conflict copy behind.
    syncer, file_node, download_path, _ = _setup_conflict(tmp_path, "rename")
    # Align the local mtime with the cached timemodified the heuristic compares
    # against, mimicking a file that was downloaded but never edited locally.
    os.utime(download_path, (1710000300, 1710000300))
    new_body = _add_new_remote(syncer)

    def boom(path, etag):
        raise OSError("cannot read file for hashing")

    monkeypatch.setattr(syncer, "_local_file_matches_etag", boom)

    assert syncer.download_file(file_node) is True
    assert download_path.read_bytes() == new_body
    assert list(download_path.parent.glob("*.syncconflict.*")) == []
