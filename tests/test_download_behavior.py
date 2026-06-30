import hashlib
import os

from syncmymoodle.__main__ import Node

from .helpers import FakeResponse, FakeSession, build_single_file_tree, make_syncer

URL = (
    "https://moodle.rwth-aachen.de/pluginfile.php/301/mod_resource/content/1/slides.pdf"
)


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def seed_course_cache(config, *, timemodified, etag, is_downloaded=True):
    """Write a per-course cache to disk describing a previously synced file.

    A real cache is written after the download walk, so a file that was
    successfully fetched is marked is_downloaded=True; pass False to simulate a
    previous run whose download failed.
    """
    cache_syncer = make_syncer(config)
    cached_root, cached_file = build_single_file_tree(
        "slides.pdf", URL, timemodified=timemodified, etag=etag
    )
    cached_file.is_downloaded = is_downloaded
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
    # The temp part-file and its etag sidecar are cleaned up on completion.
    assert list(download_path.parent.glob(".*.smmpart*")) == []
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


def test_failed_previous_download_is_retried_not_skipped(tmp_path):
    # The cache records Moodle's timemodified even when the previous download
    # failed (is_downloaded=False). Such an entry must not suppress a retry,
    # otherwise a stale file would be kept forever.
    config = {"basedir": str(tmp_path), "updatefiles": True}
    seed_course_cache(config, timemodified=1710000300, etag=None, is_downloaded=False)
    syncer, file_node = make_run_syncer(config, timemodified=1710000300)
    download_path = syncer.get_sanitized_node_path(file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(b"OLD STALE VERSION")
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf"}, chunks=[b"NEW CORRECT VERSION"]
        ),
    )

    assert syncer.download_file(file_node) is True
    assert syncer.session.count("GET", URL) == 1
    assert download_path.read_bytes() == b"NEW CORRECT VERSION"


def test_successful_previous_download_with_same_timemodified_is_skipped(tmp_path):
    # The complement: a downloaded cache entry with an unchanged timemodified is
    # still skipped without contacting the server.
    config = {"basedir": str(tmp_path), "updatefiles": True}
    seed_course_cache(config, timemodified=1710000300, etag=None, is_downloaded=True)
    syncer, file_node = make_run_syncer(config, timemodified=1710000300)
    download_path = syncer.get_sanitized_node_path(file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(b"already downloaded")

    assert syncer.download_file(file_node) is True
    assert syncer.session.calls == []


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


# --------------------------------------------------------------------------
# Failed/aborted update must not empty the canonical path (bug #1)
# --------------------------------------------------------------------------


def test_rename_conflict_failed_html_update_preserves_canonical_file(tmp_path):
    # An expired session returns an HTML login page that masquerades as a new
    # version. The download is rejected; the user's file must stay in place and
    # not be displaced to a side-car (which would empty the canonical path).
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "rename"
    )
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "text/html"},
            text="<!doctype html><html>login</html>",
        ),
    )

    assert syncer.download_file(file_node) is False
    assert download_path.exists()
    assert download_path.read_bytes() == local_modified
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def test_rename_conflict_non_2xx_update_preserves_canonical_file(tmp_path):
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "rename"
    )
    syncer.session.add("GET", URL, FakeResponse(status_code=403, text="forbidden"))

    assert syncer.download_file(file_node) is False
    assert download_path.read_bytes() == local_modified
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def test_excluded_filetype_existing_file_is_not_touched(tmp_path):
    # Exclusions are honored before any conflict handling, so an excluded file
    # that already exists is never displaced or downloaded.
    config = {
        "basedir": str(tmp_path),
        "updatefiles": True,
        "update_files_conflict": "rename",
        "exclude_filetypes": ["pdf"],
    }
    seed_course_cache(config, timemodified=1710000300, etag=sha1(b"original"))
    syncer, file_node = make_run_syncer(config, timemodified=1710000400)
    download_path = syncer.get_sanitized_node_path(file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(b"locally edited content")

    # No GET route registered: a request would raise in the fake session.
    assert syncer.download_file(file_node) is True
    assert download_path.read_bytes() == b"locally edited content"
    assert syncer.session.calls == []
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


# --------------------------------------------------------------------------
# Safe resume of partial downloads (bug #2)
# --------------------------------------------------------------------------


def _seed_partial(syncer, file_node, body, etag):
    """Write a hidden partial download plus its etag sidecar for ``file_node``."""
    download_path = syncer.get_sanitized_node_path(file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    partial = download_path.parent / f".{download_path.name}.smmpart"
    partial.write_bytes(body)
    partial.with_name(partial.name + ".etag").write_text(etag, encoding="utf-8")
    return download_path


def test_resume_appends_when_remote_unchanged(tmp_path):
    config = {"basedir": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = _seed_partial(syncer, file_node, b"HEAD-", '"v1"')
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            status_code=206,
            headers={"Content-Type": "application/pdf", "ETag": '"v1"'},
            chunks=[b"TAIL"],
        ),
    )

    assert syncer.download_file(file_node) is True
    # The partial head is kept and the resumed tail appended.
    assert download_path.read_bytes() == b"HEAD-TAIL"
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_resume_discards_partial_when_remote_served_full_content(tmp_path):
    # If-Range honored: the remote changed, so the server sends a 200 with the
    # full new body. The stale partial must be discarded, not appended to.
    config = {"basedir": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = _seed_partial(syncer, file_node, b"OLD-PARTIAL", '"v1"')
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            status_code=200,
            headers={"Content-Type": "application/pdf", "ETag": '"v2"'},
            chunks=[b"FULL-NEW-CONTENT"],
        ),
    )

    assert syncer.download_file(file_node) is True
    assert download_path.read_bytes() == b"FULL-NEW-CONTENT"
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_resume_aborts_when_server_ignores_if_range(tmp_path):
    # Some servers honor Range but ignore If-Range, returning a 206 tail of a
    # changed file. The mismatched ETag must be detected so we discard the
    # partial and retry fresh next run instead of corrupting the file.
    config = {"basedir": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = _seed_partial(syncer, file_node, b"OLD-PARTIAL", '"v1"')
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            status_code=206,
            headers={"Content-Type": "application/pdf", "ETag": '"v2"'},
            chunks=[b"TAIL-OF-NEW-VERSION"],
        ),
    )

    assert syncer.download_file(file_node) is False
    # Nothing corrupt is left behind; the stale partial is gone.
    assert not download_path.exists()
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_unrecognized_partial_without_sidecar_is_not_resumed(tmp_path):
    # A leftover partial with no etag sidecar cannot be validated, so it is
    # discarded and a fresh full download is performed.
    config = {"basedir": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = syncer.get_sanitized_node_path(file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    (download_path.parent / f".{download_path.name}.smmpart").write_bytes(b"STALE")
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[b"FRESH"]),
    )

    assert syncer.download_file(file_node) is True
    assert download_path.read_bytes() == b"FRESH"
    assert list(download_path.parent.glob(".*.smmpart*")) == []


# --------------------------------------------------------------------------
# Sciebo change detection via ETag (no timemodified) (bug #8)
# --------------------------------------------------------------------------

SCIEBO_URL = "https://rwth-aachen.sciebo.de/public.php/webdav/notes.pdf"


def _sciebo_tree(etag, is_downloaded=False):
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    course = semester.add_child("Download Course", 301, "Course")
    section = course.add_child("General", 401, "Section")
    file_node = section.add_child(
        "notes.pdf",
        None,
        "Sciebo File",
        url=SCIEBO_URL,
        additional_info={"Authorization": "Basic x"},
        etag=etag,
    )
    file_node.is_downloaded = is_downloaded
    return root, file_node


def _seed_sciebo_cache(config, etag, content):
    cache_syncer = make_syncer(config)
    root, file_node = _sciebo_tree(etag, is_downloaded=True)
    cache_syncer.root_node = root
    cache_syncer.cache_root_node()
    download_path = make_syncer(config).get_sanitized_node_path(file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(content)
    return download_path


def test_sciebo_changed_etag_triggers_redownload(tmp_path):
    # Sciebo files have no timemodified, so a changed ETag is the only signal.
    config = {"basedir": str(tmp_path), "updatefiles": True}
    old = b"old sciebo content"
    download_path = _seed_sciebo_cache(config, sha1(old), old)
    syncer = make_syncer(config)
    syncer.session = FakeSession()
    new = b"new sciebo content"
    syncer.session.add(
        "GET",
        SCIEBO_URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[new]),
    )
    _, current = _sciebo_tree(sha1(new))

    assert syncer.download_file(current) is True
    assert syncer.session.count("GET", SCIEBO_URL) == 1
    assert download_path.read_bytes() == new
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def test_sciebo_unchanged_etag_skips_download(tmp_path):
    config = {"basedir": str(tmp_path), "updatefiles": True}
    content = b"sciebo content"
    download_path = _seed_sciebo_cache(config, sha1(content), content)
    syncer = make_syncer(config)
    syncer.session = FakeSession()
    _, current = _sciebo_tree(sha1(content))  # unchanged etag

    assert syncer.download_file(current) is True
    assert syncer.session.calls == []
    assert download_path.read_bytes() == content


# --------------------------------------------------------------------------
# Cache reflects on-disk state, not optimistic Moodle markers (refinement)
# --------------------------------------------------------------------------


def _cached_file_node(config, course_node):
    cached_course = make_syncer(config)._get_course_cache_root(course_node)
    return cached_course.children[0].children[0]  # General -> slides.pdf


def test_cache_preserves_markers_for_failed_download_over_existing_file(tmp_path):
    config = {"basedir": str(tmp_path), "updatefiles": True}
    v1 = b"version one"
    seed_course_cache(config, timemodified=100, etag=sha1(v1), is_downloaded=True)
    download_path = make_syncer(config).get_sanitized_node_path(
        build_single_file_tree("slides.pdf", URL)[1]
    )
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(v1)

    # A run where Moodle reports a new version (200) but the download did not
    # happen (is_downloaded=False) and the old file is still on disk.
    syncer = make_syncer(config)
    root, file_node = build_single_file_tree(
        "slides.pdf", URL, timemodified=200, etag="poisoned"
    )
    file_node.is_downloaded = False
    syncer.root_node = root
    syncer.cache_root_node()

    cached_file = _cached_file_node(config, root.children[0].children[0])
    # The cache keeps the on-disk version's markers, not Moodle's new ones.
    assert cached_file.timemodified == 100
    assert cached_file.etag == sha1(v1)
    assert cached_file.is_downloaded is True


def test_cache_does_not_preserve_markers_when_file_absent(tmp_path):
    config = {"basedir": str(tmp_path), "updatefiles": True}
    seed_course_cache(config, timemodified=100, etag="old", is_downloaded=True)

    # Failed download with no file on disk: nothing to preserve.
    syncer = make_syncer(config)
    root, file_node = build_single_file_tree(
        "slides.pdf", URL, timemodified=200, etag="new"
    )
    file_node.is_downloaded = False
    syncer.root_node = root
    syncer.cache_root_node()

    cached_file = _cached_file_node(config, root.children[0].children[0])
    assert cached_file.timemodified == 200
    assert cached_file.is_downloaded is False
