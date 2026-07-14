import hashlib
import logging
import os

import pytest
import requests

from syncmymoodle import course_cache, downloader, links, moodle, pathing
from syncmymoodle.constants import COURSE_CACHE_FILENAME, YOUTUBE_WATCH_URL
from syncmymoodle.downloader import download_file
from syncmymoodle.node import Node, RemoteMarkerKind
from syncmymoodle.storage import write_private_gzip_json

from .helpers import (
    FakeResponse,
    FakeSession,
    build_single_file_tree,
    make_context,
    node_path,
)

URL = (
    "https://moodle.rwth-aachen.de/pluginfile.php/301/mod_resource/content/1/slides.pdf"
)
DUPLICATE_SECTION_URL_A = (
    "https://moodle.rwth-aachen.de/pluginfile.php/501/mod_resource/content/1/"
    "Case%20Study%20Sezen.pdf"
)
DUPLICATE_SECTION_URL_B = (
    "https://moodle.rwth-aachen.de/pluginfile.php/502/mod_resource/content/1/"
    "Case%20Study%20Sezen.pdf"
)


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_classify_local_file_is_tristate(tmp_path):
    from syncmymoodle.downloader import FileMatch, classify_local_file

    f = tmp_path / "f.bin"
    f.write_bytes(b"hello world\n")
    hello_world_sha1 = sha1(b"hello world\n")

    assert classify_local_file(f, hello_world_sha1) is FileMatch.MATCH
    assert classify_local_file(f, sha256(b"hello world\n")) is FileMatch.MATCH
    assert classify_local_file(f, f'"{hello_world_sha1}"') is FileMatch.MATCH
    assert classify_local_file(f, sha1(b"other")) is FileMatch.DIFFER

    assert classify_local_file(f, '"66a1b2c3d4e5"') is FileMatch.UNKNOWN
    assert classify_local_file(f, None) is FileMatch.UNKNOWN
    assert classify_local_file(f, "") is FileMatch.UNKNOWN
    assert classify_local_file(tmp_path / "nope", sha1(b"x")) is FileMatch.UNKNOWN


def test_transfer_plan_applies_windows_prefix_to_appended_temp_paths(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(pathing, "is_windows", lambda: True)
    monkeypatch.setattr(pathing, "WINDOWS_EXTENDED_PATH_THRESHOLD", 1)

    plan = downloader.prepare_transfer_plan(
        Node("file.pdf", "id", "Linked file [application/pdf]", None),
        tmp_path / "file.pdf",
    )

    assert os.fspath(plan.tmp_path).startswith("\\\\?\\")
    assert os.fspath(plan.etag_sidecar).startswith("\\\\?\\")


def test_youtube_outtmpl_applies_windows_prefix_after_template_suffix(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(pathing, "is_windows", lambda: True)
    monkeypatch.setattr(pathing, "WINDOWS_EXTENDED_PATH_THRESHOLD", 100_000)
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, opts):
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, links):
            captured["links"] = links

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    ctx = make_context({"paths.sync_directory": str(tmp_path)})
    root = Node("", -1, "Root", None)
    section = root.add_child("Section", 1, "Section")
    assert section is not None
    node = section.add_child(
        "Video",
        "video-id",
        "Youtube",
        url="https://youtu.be/abcdefghijk",
    )
    assert node is not None

    assert downloader.scan_and_download_youtube(ctx, node)

    assert captured["opts"]["outtmpl"].startswith("\\\\?\\")
    assert "%(title)s-%(id)s.%(ext)s" in captured["opts"]["outtmpl"]
    assert captured["links"] == ["https://youtu.be/abcdefghijk"]


def test_youtube_partial_file_does_not_block_resume(tmp_path, monkeypatch):
    ctx = make_context({"paths.sync_directory": str(tmp_path)})
    _, section, video_node = build_youtube_tree("https://youtu.be/abcdefghijk")
    video_path = node_path(ctx, section)
    video_path.mkdir(parents=True)
    (video_path / "Lecture-abcdefghijk.mp4.part").write_bytes(b"partial")
    (video_path / "Lecture-abcdefghijk.webp").write_bytes(b"thumbnail")
    downloads = []

    class FakeYoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, urls):
            downloads.append(urls)

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    assert downloader.scan_and_download_youtube(ctx, video_node) is True
    assert downloads == [["https://www.youtube.com/watch?v=abcdefghijk"]]


def test_tokenized_request_failure_does_not_log_token(caplog):
    secret = "request-token-must-not-leak"
    ctx = make_context()
    ctx.session = moodle.create_token_session(
        moodle.MoodleTokens(
            "fake-user",
            secret,
            "private-token",
            moodle_user_id=10001,
        )
    )
    _, file_node = build_single_file_tree("slides.pdf", URL)

    class FailingAdapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            raise requests.ConnectionError(
                f"failed request to {request.url}",
                request=request,
            )

        def close(self):
            pass

    ctx.session.mount("https://", FailingAdapter())
    caplog.set_level(logging.WARNING, logger="syncmymoodle.downloader")

    assert (
        downloader.conditional_get_confirms_unchanged(ctx, file_node, '"old"') is False
    )
    assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_human_readable_size_formats_binary_units():
    assert downloader.human_readable_size(10) == "10 B"
    assert downloader.human_readable_size(1024) == "1 KiB"
    assert downloader.human_readable_size(1536) == "1.5 KiB"
    assert downloader.human_readable_size(5 * 1024**2) == "5 MiB"
    assert downloader.human_readable_size(1024**5) == "1 PiB"


def seed_course_cache(config, *, timemodified, etag, handled=True):
    """Write a per-course cache to disk describing a previously synced file.

    A real cache is written after the download walk, so a file that was
    successfully fetched is handled; pass False to simulate a previous run
    whose download failed.
    """
    cache_syncer = make_context(config)
    cached_root, cached_file = build_single_file_tree(
        "slides.pdf", URL, timemodified=timemodified, etag=etag
    )
    if handled:
        cached_file.mark_handled()
    cache_syncer.root_node = cached_root
    course_cache.cache_root_node(cache_syncer)


def make_run_syncer(config, *, timemodified, etag=None, remote_size=None):
    """Return a syncer plus the leaf node for the current (changed) sync run."""
    syncer = make_context(config)
    syncer.session = FakeSession()
    _, file_node = build_single_file_tree(
        "slides.pdf",
        URL,
        timemodified=timemodified,
        etag=etag,
        remote_size=remote_size,
    )
    return syncer, file_node


def build_duplicate_section_file_tree():
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    course = semester.add_child("Duplicate Section Course", 301, "Course")
    first_section = course.add_child("Case Study", 501, "Section")
    first_file = first_section.add_child(
        "Case Study Sezen.pdf",
        DUPLICATE_SECTION_URL_A,
        "Linked file [application/pdf]",
        url=DUPLICATE_SECTION_URL_A,
        timemodified=100,
        name_clash_id=None,
    )
    second_section = course.add_child("Case Study", 502, "Section")
    second_file = second_section.add_child(
        "Case Study Sezen.pdf",
        DUPLICATE_SECTION_URL_B,
        "Linked file [application/pdf]",
        url=DUPLICATE_SECTION_URL_B,
        timemodified=200,
        name_clash_id=None,
    )
    return root, first_file, second_file


def build_youtube_tree(link):
    video_id = links.youtube_video_id_from_node(
        Node("Video", link, "Youtube", None, url=link)
    )
    url = YOUTUBE_WATCH_URL.format(video_id=video_id) if video_id is not None else link
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    course = semester.add_child("Video Course", 301, "Course")
    section = course.add_child("General", 401, "Section")
    video = section.add_child("Video", video_id or link, "Youtube", url=url)
    return root, section, video


# --------------------------------------------------------------------------
# Actual download happy path (gap 2)
# --------------------------------------------------------------------------


def test_download_streams_chunks_to_disk_and_records_metadata(tmp_path):
    config = {"paths.sync_directory": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = node_path(syncer, file_node)
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

    assert download_file(syncer, file_node) is True

    assert download_path.read_bytes() == b"".join(chunks)
    # The temp part-file and its etag sidecar are cleaned up on completion.
    assert list(download_path.parent.glob(".*.smmpart*")) == []
    # mtime is aligned with Moodle's timemodified so later runs detect changes.
    assert int(download_path.stat().st_mtime) == 1710000500
    # The ETag is persisted on the node for the next run's change detection.
    assert file_node.etag == etag
    assert syncer.session.count("GET", URL) == 1


def test_download_is_skipped_for_excluded_filetypes(tmp_path):
    config = {
        "paths.sync_directory": str(tmp_path),
        "filters.exclude_filetypes": ["pdf"],
    }
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = node_path(syncer, file_node)

    # No GET route registered: a request would raise in the fake session.
    assert download_file(syncer, file_node) is True
    assert not download_path.exists()
    assert syncer.session.calls == []


def test_download_is_skipped_when_max_file_size_is_exceeded(tmp_path, caplog):
    config = {"paths.sync_directory": str(tmp_path), "filters.max_file_size": "1K"}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = node_path(syncer, file_node)
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf", "content-length": "2048"},
            chunks=[b"x" * 2048],
        ),
    )

    caplog.set_level(logging.WARNING, logger="syncmymoodle.downloader")
    assert download_file(syncer, file_node) is True
    assert not download_path.exists()
    assert file_node.remote_size == 2048
    assert "known size" not in caplog.text
    assert "size (2 KiB) exceeds max_file_size (1 KiB)" in caplog.text
    assert "2048 bytes" not in caplog.text


def test_download_uses_known_remote_size_before_get(tmp_path):
    config = {"paths.sync_directory": str(tmp_path), "filters.max_file_size": "1K"}
    syncer, file_node = make_run_syncer(
        config, timemodified=1710000500, remote_size=2048
    )
    download_path = node_path(syncer, file_node)

    # No GET route registered: the known size is enough to skip.
    assert download_file(syncer, file_node) is True
    assert not download_path.exists()
    assert syncer.session.calls == []


def test_download_is_skipped_when_below_min_file_size(tmp_path):
    config = {"paths.sync_directory": str(tmp_path), "filters.min_file_size": "1K"}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = node_path(syncer, file_node)
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf", "content-length": "10"},
            chunks=[b"x" * 10],
        ),
    )

    assert download_file(syncer, file_node) is True
    assert not download_path.exists()
    assert file_node.remote_size == 10


def test_max_file_size_skips_large_youtube_videos(tmp_path, monkeypatch):
    config = {"paths.sync_directory": str(tmp_path), "filters.max_file_size": "1M"}
    syncer = make_context(config)
    link = "https://youtu.be/abcdefghijk"
    _, section, video_node = build_youtube_tree(link)

    class FakeYoutubeDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            assert download is False
            return {"filesize_approx": 5 * 1024**2}

        def download(self, urls):
            raise AssertionError("oversized video must not be downloaded")

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    assert downloader.scan_and_download_youtube(syncer, video_node) is True
    assert not node_path(syncer, section).exists()
    assert video_node.remote_size == 5 * 1024**2


def test_cached_youtube_size_skips_without_yt_dlp(tmp_path, monkeypatch):
    config = {"paths.sync_directory": str(tmp_path), "filters.max_file_size": "1M"}
    cached_link = "https://youtu.be/abcdefghijk"
    current_link = "https://www.youtube.com/embed/abcdefghijk"

    cache_syncer = make_context(config)
    cached_root, _, cached_video = build_youtube_tree(cached_link)
    cached_video.name = f"Youtube: {cached_link}"
    cached_video.id = cached_link
    cached_video.name_clash_id = cached_link
    cached_video.url = cached_link
    cached_video.remote_size = 5 * 1024**2
    cached_video.mark_handled()
    cache_syncer.root_node = cached_root
    course_cache.cache_root_node(cache_syncer)

    syncer = make_context(config)
    _, section, video_node = build_youtube_tree(current_link)

    class FakeYoutubeDL:
        def __init__(self, opts):
            raise AssertionError("cached oversized video must not query yt-dlp")

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    assert downloader.scan_and_download_youtube(syncer, video_node) is True
    assert not node_path(syncer, section).exists()
    assert video_node.remote_size == 5 * 1024**2


def test_dry_run_honors_youtube_size_limits(tmp_path, monkeypatch, capsys):
    config = {
        "paths.sync_directory": str(tmp_path),
        "downloads.dry_run": True,
        "filters.max_file_size": "1M",
    }
    syncer = make_context(config)
    link = "https://youtu.be/abcdefghijk"
    _, section, video_node = build_youtube_tree(link)

    class FakeYoutubeDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            assert download is False
            return {"filesize_approx": 5 * 1024**2}

        def download(self, urls):
            raise AssertionError("oversized dry-run video must not be downloaded")

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    assert downloader.scan_and_download_youtube(syncer, video_node) is True
    assert "Would download" not in capsys.readouterr().out
    assert not node_path(syncer, section).exists()
    assert video_node.remote_size == 5 * 1024**2


def test_youtube_estimated_size_sums_requested_formats():
    assert downloader.youtube_estimated_size({"filesize": 100}) == 100
    assert downloader.youtube_estimated_size({"filesize_approx": 200}) == 200
    assert (
        downloader.youtube_estimated_size(
            {"requested_formats": [{"filesize": 100}, {"filesize_approx": 50}]}
        )
        == 150
    )
    # Unknown sizes must not trigger the limit.
    assert downloader.youtube_estimated_size(None) is None
    assert downloader.youtube_estimated_size({}) is None
    assert (
        downloader.youtube_estimated_size(
            {"requested_formats": [{"filesize": 100}, {}]}
        )
        is None
    )


def test_download_within_max_file_size_proceeds(tmp_path):
    config = {"paths.sync_directory": str(tmp_path), "filters.max_file_size": "1M"}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf", "content-length": "4"},
            chunks=[b"data"],
        ),
    )

    assert download_file(syncer, file_node) is True
    assert node_path(syncer, file_node).read_bytes() == b"data"
    assert file_node.remote_size == 4


def test_invalid_content_length_does_not_abort_download(tmp_path):
    syncer, file_node = make_run_syncer(
        {"paths.sync_directory": str(tmp_path)},
        timemodified=1710000500,
    )
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf", "content-length": "invalid"},
            chunks=[b"data"],
        ),
    )

    assert download_file(syncer, file_node) is True
    assert node_path(syncer, file_node).read_bytes() == b"data"


def test_repeated_download_503_opens_origin_circuit(caplog, tmp_path):
    syncer, file_node = make_run_syncer(
        {"paths.sync_directory": str(tmp_path)},
        timemodified=1710000500,
    )
    syncer.session.add("GET", URL, FakeResponse(status_code=503))
    caplog.set_level(logging.WARNING, logger="syncmymoodle.downloader")

    for _ in range(4):
        assert download_file(syncer, file_node) is False

    assert syncer.session.count("GET", URL) == 3
    assert caplog.messages == [
        "Download origin https://moodle.rwth-aachen.de transient failure: GET "
        f"{URL} returned HTTP 503",
        "Download origin https://moodle.rwth-aachen.de transient failure: GET "
        f"{URL} returned HTTP 503",
        "Download origin https://moodle.rwth-aachen.de unavailable after 3 "
        f"consecutive transient failures: GET {URL} returned HTTP 503; skipping "
        "remaining requests for this sync",
    ]


def test_download_rejects_redirect_outside_allowed_domains(tmp_path):
    syncer, file_node = make_run_syncer(
        {
            "paths.sync_directory": str(tmp_path),
            "filters.allowed_domains": ["moodle.rwth-aachen.de"],
        },
        timemodified=1710000500,
    )
    external_url = "https://files.example.test/private.pdf"
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(status_code=302, headers={"Location": external_url}),
    )

    assert download_file(syncer, file_node) is False
    assert syncer.session.calls == [("GET", URL)]
    assert not node_path(syncer, file_node).exists()


def test_cross_origin_redirect_strips_download_credentials(tmp_path):
    external_url = "https://cdn.example.test/slides.pdf"
    syncer, file_node = make_run_syncer(
        {
            "paths.sync_directory": str(tmp_path),
            "filters.allowed_domains": [
                "moodle.rwth-aachen.de",
                "cdn.example.test",
            ],
        },
        timemodified=1710000500,
    )
    file_node.download_headers = {
        "Authorization": "Basic secret",
        "requesttoken": "request-secret",
        "Range": "bytes=0-",
    }

    def redirect_response(url, kwargs):
        assert kwargs["headers"]["Authorization"] == "Basic secret"
        return FakeResponse(status_code=302, headers={"Location": external_url})

    def download_response(url, kwargs):
        assert "Authorization" not in kwargs["headers"]
        assert "requesttoken" not in kwargs["headers"]
        assert kwargs["headers"]["Range"] == "bytes=0-"
        return FakeResponse(
            headers={"Content-Type": "application/pdf"},
            chunks=[b"data"],
        )

    syncer.session.add("GET", URL, redirect_response)
    syncer.session.add("GET", external_url, download_response)

    assert download_file(syncer, file_node) is True
    assert node_path(syncer, file_node).read_bytes() == b"data"


def test_dry_run_reports_downloads_without_writing(tmp_path, capsys):
    config = {"paths.sync_directory": str(tmp_path), "downloads.dry_run": True}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = node_path(syncer, file_node)

    # No GET route registered: any request would raise in the fake session.
    assert download_file(syncer, file_node) is True
    assert f"Would download {download_path}" in capsys.readouterr().out
    assert not download_path.exists()
    assert syncer.session.calls == []


def test_dry_run_honors_direct_download_size_limits(tmp_path, capsys):
    config = {
        "paths.sync_directory": str(tmp_path),
        "downloads.dry_run": True,
        "filters.max_file_size": "1K",
    }
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = node_path(syncer, file_node)
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf", "content-length": "2048"},
            chunks=[b"x" * 2048],
        ),
    )

    assert download_file(syncer, file_node) is True
    assert "Would download" not in capsys.readouterr().out
    assert not download_path.exists()
    assert syncer.session.count("GET", URL) == 1


def test_download_path_is_deduplicated_within_a_run(tmp_path):
    # Two distinct nodes that resolve to the same on-disk path must download
    # only once, exercising the per-run downloaded path guard.
    config = {"paths.sync_directory": str(tmp_path)}
    syncer = make_context(config)
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

    assert download_file(syncer, first_node) is True
    assert download_file(syncer, second_node) is True
    assert syncer.session.count("GET", URL) == 1


# --------------------------------------------------------------------------
# conflict_handling behavior
# --------------------------------------------------------------------------


def _setup_conflict(tmp_path, conflict_mode):
    """Cache a file, then locally modify it so the next run sees a conflict."""
    original = b"original remote content"
    local_modified = b"locally edited content"
    config = {
        "paths.sync_directory": str(tmp_path),
        "downloads.update_files": True,
        "downloads.conflict_handling": conflict_mode,
    }
    seed_course_cache(config, timemodified=1710000300, etag=sha1(original))

    syncer, file_node = make_run_syncer(config, timemodified=1710000400)
    download_path = node_path(syncer, file_node)
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
    assert download_file(syncer, file_node) is True
    assert download_path.read_bytes() == local_modified
    assert syncer.session.calls == []


def test_conflict_overwrite_replaces_local_file(tmp_path):
    syncer, file_node, download_path, _ = _setup_conflict(tmp_path, "overwrite")
    new_body = _add_new_remote(syncer)

    assert download_file(syncer, file_node) is True
    assert download_path.read_bytes() == new_body
    # Overwrite mode leaves no side-car conflict copy behind.
    assert list(download_path.parent.glob("*.syncconflict.*")) == []
    assert syncer.session.count("GET", URL) == 1


def test_conflict_rename_moves_local_file_aside_before_download(tmp_path):
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "rename"
    )
    new_body = _add_new_remote(syncer)

    assert download_file(syncer, file_node) is True

    # The fresh remote content lands at the canonical path.
    assert download_path.read_bytes() == new_body
    # The user's local edits are preserved in a side-car conflict file.
    conflicts = list(download_path.parent.glob("*.syncconflict.*"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == local_modified
    assert syncer.session.count("GET", URL) == 1


def test_unchanged_timemodified_skips_download_despite_local_edit(tmp_path):
    # When Moodle reports the same timemodified as the cache, the file is
    # considered unchanged remotely and the local copy is left untouched.
    original = b"original remote content"
    config = {
        "paths.sync_directory": str(tmp_path),
        "downloads.update_files": True,
        "downloads.conflict_handling": "rename",
    }
    seed_course_cache(config, timemodified=1710000300, etag=sha1(original))
    syncer, file_node = make_run_syncer(config, timemodified=1710000300)
    download_path = node_path(syncer, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(b"locally edited content")

    assert download_file(syncer, file_node) is True
    assert syncer.session.calls == []
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def test_failed_previous_download_is_retried_not_skipped(tmp_path):
    # The cache records Moodle's timemodified even when the previous download
    # failed (still pending). Such an entry must not suppress a retry,
    # otherwise a stale file would be kept forever.
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    seed_course_cache(config, timemodified=1710000300, etag=None, handled=False)
    syncer, file_node = make_run_syncer(config, timemodified=1710000300)
    download_path = node_path(syncer, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(b"OLD STALE VERSION")
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf"}, chunks=[b"NEW CORRECT VERSION"]
        ),
    )

    assert download_file(syncer, file_node) is True
    assert syncer.session.count("GET", URL) == 1
    assert download_path.read_bytes() == b"NEW CORRECT VERSION"


def test_unchanged_linked_file_etag_skips_download(tmp_path):
    # Direct linked files do not have Moodle timemodified metadata. The HEAD
    # ETag discovered while scanning is their remote version marker, so an
    # unchanged ETag must suppress the GET on later runs.
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    etag = '"linked-file-v1"'
    content = b"already downloaded linked file"
    seed_course_cache(config, timemodified=None, etag=etag)
    syncer, file_node = make_run_syncer(config, timemodified=None, etag=etag)
    download_path = node_path(syncer, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(content)

    assert download_file(syncer, file_node) is True
    assert syncer.session.calls == []
    assert download_path.read_bytes() == content


def test_get_only_etag_304_skips_download(tmp_path):
    # Legacy Opencast/embedded nodes may only have the ETag discovered during
    # the previous GET. If the current scan cannot provide a marker, validate
    # the cached ETag with If-None-Match before re-downloading.
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    etag = '"opencast-v1"'
    content = b"already downloaded video"
    seed_course_cache(config, timemodified=None, etag=etag)
    syncer, file_node = make_run_syncer(config, timemodified=None, etag=None)
    download_path = node_path(syncer, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(content)
    seen_headers = []

    def unchanged(url, kwargs):
        del url
        seen_headers.append(kwargs.get("headers", {}).copy())
        return FakeResponse(status_code=304, headers={"ETag": etag})

    syncer.session.add("GET", URL, unchanged)

    assert download_file(syncer, file_node) is True
    assert syncer.session.count("GET", URL) == 1
    assert seen_headers == [{"If-None-Match": etag}]
    assert file_node.etag == etag
    assert download_path.read_bytes() == content


def test_get_only_etag_same_200_skips_download(tmp_path):
    # Some servers ignore If-None-Match but still return the same ETag on a 200
    # response. Closing that streamed response without reading the body avoids
    # a needless full re-download.
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    etag = '"video-v1"'
    content = b"already downloaded video"
    seed_course_cache(config, timemodified=None, etag=etag)
    syncer, file_node = make_run_syncer(config, timemodified=None, etag=None)
    download_path = node_path(syncer, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(content)
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "video/mp4", "ETag": etag},
            chunks=[b"same remote body that should not be read"],
        ),
    )

    assert download_file(syncer, file_node) is True
    assert syncer.session.count("GET", URL) == 1
    assert file_node.etag == etag
    assert download_path.read_bytes() == content


def test_get_only_etag_changed_200_downloads_update(tmp_path):
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    original = b"already downloaded video"
    old_etag = sha1(original)
    new_etag = '"video-v2"'
    seed_course_cache(config, timemodified=None, etag=old_etag)
    syncer, file_node = make_run_syncer(config, timemodified=None, etag=None)
    download_path = node_path(syncer, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(original)
    responses = [
        FakeResponse(headers={"Content-Type": "video/mp4", "ETag": new_etag}),
        FakeResponse(
            headers={"Content-Type": "video/mp4", "ETag": new_etag},
            chunks=[b"new remote video"],
        ),
    ]

    def changed(url, kwargs):
        del url, kwargs
        return responses.pop(0)

    syncer.session.add("GET", URL, changed)

    assert download_file(syncer, file_node) is True
    assert syncer.session.count("GET", URL) == 2
    assert file_node.etag == new_etag
    assert download_path.read_bytes() == b"new remote video"


def test_unchanged_duplicate_section_file_uses_matching_cache_node(tmp_path):
    # Some Moodle courses contain duplicate same-named sections that collapse
    # onto the same local directory. Cache lookup must still match the section
    # by its stable id, otherwise the second section can inherit timestamps from
    # the first and re-download unchanged files as false conflicts.
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    content = b"same case study pdf bytes"

    cached_root, cached_first, cached_second = build_duplicate_section_file_tree()
    cached_first.mark_handled()
    cached_second.mark_handled()
    cached_second.content_hash = sha256(content)
    cache_syncer = make_context(config)
    cache_syncer.root_node = cached_root
    course_cache.cache_root_node(cache_syncer)

    syncer = make_context(config)
    syncer.session = FakeSession()
    _, _, current_second = build_duplicate_section_file_tree()
    download_path = node_path(syncer, current_second)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(content)
    os.utime(download_path, (200, 200))

    old_node = course_cache.get_old_node_for(syncer, current_second)
    assert old_node is not None
    assert old_node.url == DUPLICATE_SECTION_URL_B

    assert download_file(syncer, current_second) is True
    assert syncer.session.calls == []
    assert list(download_path.parent.glob("*.syncconflict.*")) == []
    assert download_path.read_bytes() == content


def test_distinct_files_in_merged_sections_are_both_downloaded(tmp_path):
    syncer = make_context({"paths.sync_directory": str(tmp_path)})
    syncer.session = FakeSession()
    root, first_file, second_file = build_duplicate_section_file_tree()
    root.remove_children_nameclashes()
    first_path = node_path(syncer, first_file)
    second_path = node_path(syncer, second_file)
    syncer.session.add(
        "GET",
        DUPLICATE_SECTION_URL_A,
        FakeResponse(
            headers={"Content-Type": "application/pdf"},
            chunks=[b"first section"],
        ),
    )
    syncer.session.add(
        "GET",
        DUPLICATE_SECTION_URL_B,
        FakeResponse(
            headers={"Content-Type": "application/pdf"},
            chunks=[b"second section"],
        ),
    )

    assert first_path != second_path
    assert download_file(syncer, first_file) is True
    assert download_file(syncer, second_file) is True
    assert first_path.read_bytes() == b"first section"
    assert second_path.read_bytes() == b"second section"


def test_etag_failure_falls_back_to_timestamp_heuristic_conflict(tmp_path, monkeypatch):
    # A faulty ETag cache is treated as if there were no cached ETag, so the
    # timestamp heuristic decides. Here the local mtime differs from the cached
    # Moodle timestamp, so it is a conflict and the local edits are kept aside.
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "rename"
    )
    new_body = _add_new_remote(syncer)

    def unverifiable(path, marker):
        return downloader.FileMatch.UNKNOWN

    monkeypatch.setattr("syncmymoodle.downloader.classify_local_file", unverifiable)

    assert download_file(syncer, file_node) is True
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

    def unverifiable(path, marker):
        return downloader.FileMatch.UNKNOWN

    monkeypatch.setattr("syncmymoodle.downloader.classify_local_file", unverifiable)

    assert download_file(syncer, file_node) is True
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

    assert download_file(syncer, file_node) is False
    assert download_path.exists()
    assert download_path.read_bytes() == local_modified
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def test_rename_conflict_non_2xx_update_preserves_canonical_file(tmp_path):
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "rename"
    )
    syncer.session.add("GET", URL, FakeResponse(status_code=403, text="forbidden"))

    assert download_file(syncer, file_node) is False
    assert download_path.read_bytes() == local_modified
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def test_excluded_filetype_existing_file_is_not_touched(tmp_path):
    # Exclusions are honored before any conflict handling, so an excluded file
    # that already exists is never displaced or downloaded.
    config = {
        "paths.sync_directory": str(tmp_path),
        "downloads.update_files": True,
        "downloads.conflict_handling": "rename",
        "filters.exclude_filetypes": ["pdf"],
    }
    seed_course_cache(config, timemodified=1710000300, etag=sha1(b"original"))
    syncer, file_node = make_run_syncer(config, timemodified=1710000400)
    download_path = node_path(syncer, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(b"locally edited content")

    # No GET route registered: a request would raise in the fake session.
    assert download_file(syncer, file_node) is True
    assert download_path.read_bytes() == b"locally edited content"
    assert syncer.session.calls == []
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


# --------------------------------------------------------------------------
# Safe resume of partial downloads (bug #2)
# --------------------------------------------------------------------------


def _seed_partial(syncer, file_node, body, etag):
    """Write a hidden partial download plus its etag sidecar for ``file_node``."""
    download_path = node_path(syncer, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    partial = download_path.parent / f".{download_path.name}.smmpart"
    partial.write_bytes(body)
    partial.with_name(partial.name + ".etag").write_text(etag, encoding="utf-8")
    return download_path


def test_resume_appends_when_remote_unchanged(tmp_path):
    config = {"paths.sync_directory": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = _seed_partial(syncer, file_node, b"HEAD-", '"v1"')
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            status_code=206,
            headers={
                "Content-Type": "application/pdf",
                "Content-Range": "bytes 5-8/9",
                "ETag": '"v1"',
            },
            chunks=[b"TAIL"],
        ),
    )

    assert download_file(syncer, file_node) is True
    # The partial head is kept and the resumed tail appended.
    assert download_path.read_bytes() == b"HEAD-TAIL"
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_resume_aborts_when_partial_response_starts_at_wrong_offset(tmp_path):
    config = {"paths.sync_directory": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = _seed_partial(syncer, file_node, b"HEAD-", '"v1"')
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            status_code=206,
            headers={
                "Content-Type": "application/pdf",
                "Content-Range": "bytes 0-3/4",
                "ETag": '"v1"',
            },
            chunks=[b"FULL"],
        ),
    )

    assert download_file(syncer, file_node) is False
    assert not download_path.exists()
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_resume_discards_partial_when_remote_served_full_content(tmp_path):
    # If-Range honored: the remote changed, so the server sends a 200 with the
    # full new body. The stale partial must be discarded, not appended to.
    config = {"paths.sync_directory": str(tmp_path)}
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

    assert download_file(syncer, file_node) is True
    assert download_path.read_bytes() == b"FULL-NEW-CONTENT"
    assert list(download_path.parent.glob(".*.smmpart*")) == []


@pytest.mark.parametrize(
    "response_etag",
    ['"v2"', None],
    ids=["mismatched-etag", "missing-etag"],
)
def test_resume_aborts_when_partial_etag_cannot_be_verified(tmp_path, response_etag):
    # A changed or missing response ETag cannot prove that the returned tail
    # belongs to the same remote version as the saved partial.
    config = {"paths.sync_directory": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = _seed_partial(syncer, file_node, b"OLD-PARTIAL", '"v1"')
    headers = {
        "Content-Type": "application/pdf",
        "Content-Range": "bytes 11-14/15",
    }
    if response_etag is not None:
        headers["ETag"] = response_etag
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            status_code=206,
            headers=headers,
            chunks=[b"TAIL"],
        ),
    )

    assert download_file(syncer, file_node) is False
    assert not download_path.exists()
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_unrecognized_partial_without_sidecar_is_not_resumed(tmp_path):
    # A leftover partial with no etag sidecar cannot be validated, so it is
    # discarded and a fresh full download is performed.
    config = {"paths.sync_directory": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = node_path(syncer, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    (download_path.parent / f".{download_path.name}.smmpart").write_bytes(b"STALE")
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[b"FRESH"]),
    )

    assert download_file(syncer, file_node) is True
    assert download_path.read_bytes() == b"FRESH"
    assert list(download_path.parent.glob(".*.smmpart*")) == []


# --------------------------------------------------------------------------
# Sciebo change detection via ETag (no timemodified) (bug #8)
# --------------------------------------------------------------------------

SCIEBO_URL = "https://rwth-aachen.sciebo.de/public.php/webdav/notes.pdf"


def _sciebo_tree(etag, handled=False, content_hash=None, etag_kind=None):
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    course = semester.add_child("Download Course", 301, "Course")
    section = course.add_child("General", 401, "Section")
    file_node = section.add_child(
        "notes.pdf",
        None,
        "Sciebo File",
        url=SCIEBO_URL,
        download_headers={"Authorization": "Basic x"},
        etag=etag,
        etag_kind=etag_kind,
    )
    if handled:
        file_node.mark_handled()
    file_node.content_hash = content_hash
    return root, file_node


def _seed_sciebo_cache(config, etag, content, content_hash=None, etag_kind=None):
    cache_syncer = make_context(config)
    root, file_node = _sciebo_tree(
        etag,
        handled=True,
        content_hash=content_hash,
        etag_kind=etag_kind,
    )
    cache_syncer.root_node = root
    course_cache.cache_root_node(cache_syncer)
    download_path = node_path(make_context(config), file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(content)
    return download_path


# An opaque Nextcloud/WebDAV revision token (what Sciebo returns for files that
# have no oc:checksums entry). It is NOT a content hash, so it cannot be used to
# verify local file contents.
GETETAG_V1 = '"665f1a2b3c4d5"'
GETETAG_V2 = '"67a09e8d7c6b5"'


def test_sciebo_changed_etag_triggers_redownload(tmp_path):
    # Sciebo files have no timemodified, so a changed ETag is the only signal.
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    old = b"old sciebo content"
    download_path = _seed_sciebo_cache(config, sha1(old), old)
    syncer = make_context(config)
    syncer.session = FakeSession()
    new = b"new sciebo content"
    syncer.session.add(
        "GET",
        SCIEBO_URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[new]),
    )
    _, current = _sciebo_tree(sha1(new))

    assert download_file(syncer, current) is True
    assert syncer.session.count("GET", SCIEBO_URL) == 1
    assert download_path.read_bytes() == new
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def test_sciebo_unchanged_etag_skips_download(tmp_path):
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    content = b"sciebo content"
    download_path = _seed_sciebo_cache(config, sha1(content), content)
    syncer = make_context(config)
    syncer.session = FakeSession()
    _, current = _sciebo_tree(sha1(content))  # unchanged etag

    assert download_file(syncer, current) is True
    assert syncer.session.calls == []
    assert download_path.read_bytes() == content


def test_sciebo_unchanged_opaque_getetag_skips_without_conflict(tmp_path):
    # Regression: files without oc:checksums fall back to an opaque getetag,
    # which is not a content hash. An unchanged getetag must skip cleanly rather
    # than re-download and move the identical local copy aside as a conflict on
    # every run.
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    content = b"post-quantum notes"
    download_path = _seed_sciebo_cache(config, GETETAG_V1, content)
    syncer = make_context(config)
    syncer.session = FakeSession()
    _, current = _sciebo_tree(GETETAG_V1)  # same opaque getetag

    assert download_file(syncer, current) is True
    assert syncer.session.calls == []
    assert download_path.read_bytes() == content
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def test_sciebo_download_keeps_propfind_etag_when_get_etag_differs(tmp_path):
    # The next sync discovers Sciebo files through PROPFIND again, so the
    # cached version marker must stay comparable to the PROPFIND value. Some
    # WebDAV downloads return a different GET ETag for the same file.
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    download_path = _seed_sciebo_cache(config, GETETAG_V1, b"old")
    syncer = make_context(config)
    syncer.session = FakeSession()
    new = b"new content"
    syncer.session.add(
        "GET",
        SCIEBO_URL,
        FakeResponse(
            headers={
                "Content-Type": "application/pdf",
                "ETag": '"different-get-etag"',
            },
            chunks=[new],
        ),
    )
    _, current = _sciebo_tree(GETETAG_V2)

    assert download_file(syncer, current) is True
    assert download_path.read_bytes() == new
    assert current.etag == GETETAG_V2
    assert current.content_hash == sha256(new)


def test_sciebo_changed_getetag_without_local_edit_overwrites_cleanly(tmp_path):
    # Remote changed but the user did not touch the local file (it matches the
    # stored content hash): overwrite without a spurious conflict copy.
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    original = b"our downloaded copy"
    download_path = _seed_sciebo_cache(
        config, GETETAG_V1, original, content_hash=sha256(original)
    )
    syncer = make_context(config)
    syncer.session = FakeSession()
    new = b"remote v2"
    syncer.session.add(
        "GET",
        SCIEBO_URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[new]),
    )
    _, current = _sciebo_tree(GETETAG_V2)

    assert download_file(syncer, current) is True
    assert download_path.read_bytes() == new
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def test_sciebo_changed_getetag_with_local_edit_preserves_conflict(tmp_path):
    # Remote changed AND the user edited the local file (it no longer matches the
    # stored content hash): preserve the user's version as a conflict copy.
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    original = b"our downloaded copy"
    download_path = _seed_sciebo_cache(
        config, GETETAG_V1, original, content_hash=sha256(original)
    )
    edited = b"user edited this locally"
    download_path.write_bytes(edited)

    syncer = make_context(config)
    syncer.session = FakeSession()
    new = b"remote v2"
    syncer.session.add(
        "GET",
        SCIEBO_URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[new]),
    )
    _, current = _sciebo_tree(GETETAG_V2)

    assert download_file(syncer, current) is True
    assert download_path.read_bytes() == new
    conflicts = list(download_path.parent.glob("*.syncconflict.*"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == edited


def test_opaque_etag_is_not_treated_as_local_content_hash(tmp_path):
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    original = b"etag-looking marker is not a content proof"
    old_marker = sha1(original)
    download_path = _seed_sciebo_cache(
        config,
        old_marker,
        original,
        etag_kind=RemoteMarkerKind.OPAQUE,
    )

    syncer = make_context(config)
    syncer.session = FakeSession()
    new = b"remote v2"
    syncer.session.add(
        "GET",
        SCIEBO_URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[new]),
    )
    _, current = _sciebo_tree(GETETAG_V2, etag_kind=RemoteMarkerKind.OPAQUE)

    assert download_file(syncer, current) is True
    assert download_path.read_bytes() == new
    conflicts = list(download_path.parent.glob("*.syncconflict.*"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == original


# --------------------------------------------------------------------------
# Cache reflects on-disk state, not optimistic Moodle markers (refinement)
# --------------------------------------------------------------------------


def _cached_file_node(config, course_node):
    cached_course = course_cache.get_course_cache_root(
        make_context(config), course_node
    )
    assert cached_course is not None
    return cached_course.children[0].children[0]  # General -> slides.pdf


def test_cache_preserves_markers_for_failed_download_over_existing_file(tmp_path):
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    v1 = b"version one"
    seed_course_cache(config, timemodified=100, etag=sha1(v1))
    download_path = node_path(
        make_context(config), build_single_file_tree("slides.pdf", URL)[1]
    )
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(v1)

    # A run where Moodle reports a new version (200) but the download did not
    # happen (the node is still pending) and the old file is still on disk.
    syncer = make_context(config)
    root, _ = build_single_file_tree(
        "slides.pdf", URL, timemodified=200, etag="poisoned"
    )
    syncer.root_node = root
    course_cache.cache_root_node(syncer)

    cached_file = _cached_file_node(config, root.children[0].children[0])
    # The cache keeps the on-disk version's markers, not Moodle's new ones.
    assert cached_file.timemodified == 100
    assert cached_file.etag == sha1(v1)
    assert cached_file.is_handled is True


def test_legacy_is_downloaded_cache_key_is_read_as_handled(tmp_path):
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    content = b"legacy cached file"
    root, cached_file = build_single_file_tree(
        "slides.pdf", URL, timemodified=100, etag=sha1(content)
    )
    course_node = root.children[0].children[0]
    course_path = node_path(make_context(config), course_node)
    course_path.mkdir(parents=True, exist_ok=True)
    write_private_gzip_json(
        course_path / COURSE_CACHE_FILENAME,
        {
            "format": course_cache.COURSE_CACHE_FORMAT,
            "course": {
                "name": course_node.name,
                "id": course_node.id,
                "type": course_node.type,
                "children": [
                    {
                        "name": cached_file.parent.name,
                        "id": cached_file.parent.id,
                        "type": cached_file.parent.type,
                        "children": [
                            {
                                "name": cached_file.name,
                                "id": cached_file.id,
                                "type": cached_file.type,
                                "url": cached_file.url,
                                "timemodified": cached_file.timemodified,
                                "etag": cached_file.etag,
                                "is_downloaded": True,
                            }
                        ],
                    }
                ],
            },
        },
    )

    syncer, current_file = make_run_syncer(config, timemodified=100)
    download_path = node_path(syncer, current_file)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(content)

    assert download_file(syncer, current_file) is True
    assert syncer.session.calls == []
    assert download_path.read_bytes() == content
    assert course_cache.get_old_node_for(syncer, current_file).is_handled is True


def test_course_cache_round_trips_remote_size(tmp_path):
    config = {"paths.sync_directory": str(tmp_path)}
    cache_syncer = make_context(config)
    cached_root, cached_file = build_single_file_tree(
        "slides.pdf", URL, timemodified=100, etag='"v1"', remote_size=2048
    )
    cached_file.mark_handled()
    cache_syncer.root_node = cached_root
    course_cache.cache_root_node(cache_syncer)

    syncer, current_file = make_run_syncer(config, timemodified=100)
    old_file = course_cache.get_old_node_for(syncer, current_file)

    assert old_file.remote_size == 2048


def test_cache_preserves_content_hash_for_skipped_existing_file(tmp_path):
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    v1 = b"version one"
    v1_hash = sha256(v1)
    cache_syncer = make_context(config)
    cached_root, cached_file = build_single_file_tree(
        "slides.pdf", URL, timemodified=100, etag='"v1"'
    )
    cached_file.mark_handled()
    cached_file.content_hash = v1_hash
    cache_syncer.root_node = cached_root
    course_cache.cache_root_node(cache_syncer)
    download_path = node_path(make_context(config), cached_file)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(v1)

    # The download walk marks an unchanged existing file as handled even though
    # no bytes were replaced. Cache writing must keep the previous content hash
    # so a later remote change can still detect local edits precisely.
    syncer = make_context(config)
    root, file_node = build_single_file_tree(
        "slides.pdf", URL, timemodified=100, etag='"v1"'
    )
    file_node.mark_handled()
    syncer.root_node = root
    course_cache.cache_root_node(syncer)

    cached_file = _cached_file_node(config, root.children[0].children[0])
    assert cached_file.timemodified == 100
    assert cached_file.etag == '"v1"'
    assert cached_file.content_hash == v1_hash
    assert cached_file.is_handled is True


def test_cache_does_not_preserve_markers_when_file_absent(tmp_path):
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    seed_course_cache(config, timemodified=100, etag="old")

    # Failed download with no file on disk: nothing to preserve.
    syncer = make_context(config)
    root, _ = build_single_file_tree("slides.pdf", URL, timemodified=200, etag="new")
    syncer.root_node = root
    course_cache.cache_root_node(syncer)

    cached_file = _cached_file_node(config, root.children[0].children[0])
    assert cached_file.timemodified == 200
    assert cached_file.is_handled is False
