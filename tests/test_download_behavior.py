import hashlib
import logging
import os
from pathlib import Path

import pytest
import requests

from syncmymoodle import (
    course_cache,
    downloader,
    links,
    moodle,
    moodle_files,
    opencast,
    pathing,
)
from syncmymoodle.constants import (
    COURSE_CACHE_FILENAME,
    YOUTUBE_WATCH_URL,
    YT_DLP_TESTED_VERSION,
)
from syncmymoodle.downloader import download_file
from syncmymoodle.node import DownloadKind, DownloadStatus, Node, RemoteMarkerKind
from syncmymoodle.outcomes import HANDLED_DOWNLOAD
from syncmymoodle.output import format_size
from syncmymoodle.storage import read_private_gzip_json, write_private_gzip_json

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
OPENCAST_VIDEO_URL = "https://video.example.test/opencast/presentation.mp4"
OPENCAST_EPISODE_ID = "11111111-2222-4333-8444-555555555555"


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
    assert classify_local_file(f, "a" * 65) is FileMatch.UNKNOWN
    assert classify_local_file(f, None) is FileMatch.UNKNOWN
    assert classify_local_file(f, "") is FileMatch.UNKNOWN
    assert classify_local_file(tmp_path / "nope", sha1(b"x")) is FileMatch.UNKNOWN


def test_conflicting_discovered_markers_force_download(tmp_path, monkeypatch):
    content = b"cached file"
    path = tmp_path / "slides.pdf"
    path.write_bytes(content)
    old = Node(
        "slides.pdf",
        2,
        "File",
        None,
        etag=sha1(content),
        etag_kind=RemoteMarkerKind.CONTENT_HASH,
        timemodified=123,
    )
    old.mark_handled()
    parent = Node("Section", 1, "Section", None)
    current = parent.add_download_child(
        "slides.pdf",
        2,
        "File",
        url=URL,
        etag=sha1(content),
        etag_kind=RemoteMarkerKind.CONTENT_HASH,
        timemodified=123,
    )
    parent.add_download_child(
        "slides.pdf",
        2,
        "File",
        url=URL,
        etag=sha1(b"different remote bytes"),
        etag_kind=RemoteMarkerKind.CONTENT_HASH,
        timemodified=123,
    )
    monkeypatch.setattr(course_cache, "get_old_node_for", lambda *args: old)
    ctx = make_context({"downloads.update_files": True})

    assert (
        downloader.decide_download(ctx, current, path)
        is downloader.DownloadDecision.DOWNLOAD
    )


def test_incompatible_marker_kinds_override_equal_timestamp(tmp_path, monkeypatch):
    content = b"cached file"
    path = tmp_path / "slides.pdf"
    path.write_bytes(content)
    old = Node(
        "slides.pdf",
        2,
        "File",
        None,
        etag=sha1(content),
        etag_kind=RemoteMarkerKind.CONTENT_HASH,
        timemodified=123,
    )
    old.mark_handled()
    current = Node(
        "slides.pdf",
        2,
        "File",
        None,
        etag='"opaque-revision"',
        etag_kind=RemoteMarkerKind.OPAQUE,
        timemodified=123,
    )
    monkeypatch.setattr(course_cache, "get_old_node_for", lambda *args: old)
    ctx = make_context({"downloads.update_files": True})

    assert (
        downloader.decide_download(ctx, current, path)
        is downloader.DownloadDecision.DOWNLOAD
    )


def test_moodle_content_hash_skips_identical_untracked_file(tmp_path):
    content = b"already downloaded Moodle file"
    ctx = make_context(
        {
            "paths.sync_directory": str(tmp_path),
            "downloads.update_files": True,
            "downloads.conflict_handling": "rename",
        }
    )
    ctx.session = FakeSession()
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    course = semester.add_child("Course", 301, "Course")
    section = course.add_child("General", 401, "Section")
    file_node = moodle_files.add_moodle_content_file_node(
        section,
        {
            "filename": "slides.pdf",
            "fileurl": URL,
            "mimetype": "application/pdf",
            "timemodified": 1710000300,
            "filesize": len(content),
            "contenthash": sha1(content),
        },
    )
    assert file_node is not None
    download_path = node_path(ctx, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(content)

    outcome = download_file(ctx, file_node)

    assert outcome.unchanged == 1
    assert file_node.etag == sha1(content)
    assert file_node.etag_kind is RemoteMarkerKind.CONTENT_HASH
    assert file_node.content_hash == sha256(content)
    assert ctx.session.calls == []
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def _uncached_local_file(
    tmp_path,
    *,
    timemodified=1710000300,
    local_mtime=1710000300,
    etag=None,
    content=b"local content",
    dry_run=False,
):
    ctx = make_context(
        {
            "paths.sync_directory": str(tmp_path),
            "downloads.update_files": True,
            "downloads.dry_run": dry_run,
        }
    )
    ctx.session = FakeSession()
    ctx.root_node, file_node = build_single_file_tree(
        "slides.pdf",
        URL,
        timemodified=timemodified,
        etag=etag,
    )
    assert file_node is not None
    download_path = node_path(ctx, file_node)
    download_path.parent.mkdir(parents=True)
    download_path.write_bytes(content)
    os.utime(download_path, (local_mtime, local_mtime))
    return ctx, file_node, download_path


def test_matching_remote_timestamp_adopts_uncached_file_and_persists_hash(tmp_path):
    content = b"already downloaded content without a remote checksum"
    ctx, file_node, _ = _uncached_local_file(tmp_path, content=content)

    assert ctx.root_node is not None
    downloader.download_node_tree(ctx, ctx.root_node)
    course_cache.cache_root_node(ctx)

    assert ctx.stats.unchanged == 1
    assert ctx.stats.planned == 0
    assert file_node.content_hash == sha256(content)
    assert ctx.session.calls == []

    loaded = make_context({"paths.sync_directory": str(tmp_path)})
    cached_file = course_cache.get_old_node_for(loaded, file_node)
    assert cached_file is not None
    assert cached_file.is_verified
    assert cached_file.content_hash == sha256(content)


def test_dry_run_reports_matching_uncached_timestamp_as_unchanged(tmp_path):
    ctx, file_node, _ = _uncached_local_file(tmp_path, dry_run=True)
    course = course_cache.get_course_node(file_node)

    outcome = download_file(ctx, file_node)

    assert outcome.unchanged == 1
    assert outcome.planned == 0
    assert ctx.session.calls == []
    assert not course_cache.course_cache_path(ctx, course).exists()


@pytest.mark.parametrize("marker", [sha1(b"different remote content"), "not-a-hash"])
def test_current_content_hash_blocks_timestamp_adoption(tmp_path, marker):
    ctx, file_node, download_path = _uncached_local_file(tmp_path, etag=marker)
    file_node.etag_kind = RemoteMarkerKind.CONTENT_HASH

    assert (
        downloader.decide_download(ctx, file_node, download_path)
        is downloader.DownloadDecision.CONFLICT
    )


def test_conflicting_remote_markers_block_timestamp_adoption(tmp_path):
    ctx, file_node, download_path = _uncached_local_file(
        tmp_path,
        etag=sha1(b"first remote content"),
    )
    file_node.etag_kind = RemoteMarkerKind.CONTENT_HASH
    assert file_node.parent is not None
    file_node.parent.add_download_child(
        file_node.name,
        file_node.id,
        file_node.type,
        url=URL,
        timemodified=1710000300,
        etag=sha1(b"second remote content"),
        etag_kind=RemoteMarkerKind.CONTENT_HASH,
    )

    assert file_node.has_remote_marker_conflict
    assert (
        downloader.decide_download(ctx, file_node, download_path)
        is downloader.DownloadDecision.CONFLICT
    )


@pytest.mark.parametrize(
    "timemodified",
    [1710000301, True, -1, "1710000300", 1710000300.0],
)
def test_nonmatching_or_invalid_remote_timestamp_is_not_adopted(tmp_path, timemodified):
    ctx, file_node, download_path = _uncached_local_file(
        tmp_path,
        timemodified=timemodified,
    )

    assert (
        downloader.decide_download(ctx, file_node, download_path)
        is downloader.DownloadDecision.CONFLICT
    )


def test_unreadable_snapshot_is_not_adopted_by_timestamp(tmp_path):
    ctx, file_node, download_path = _uncached_local_file(tmp_path)

    assert (
        downloader.decide_download(
            ctx,
            file_node,
            download_path,
            baseline=downloader.storage.FileSnapshot(True),
        )
        is downloader.DownloadDecision.CONFLICT
    )


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


def test_discard_partial_tolerates_a_windows_style_file_lock(tmp_path, monkeypatch):
    plan = downloader.prepare_transfer_plan(
        Node("file.pdf", "id", "Linked file [application/pdf]", None),
        tmp_path / "file.pdf",
    )
    plan.tmp_path.write_bytes(b"partial")
    plan.etag_sidecar.write_text('"revision-1"', encoding="utf-8")
    original_unlink = Path.unlink

    def locked_unlink(path, *args, **kwargs):
        if path == plan.tmp_path:
            raise PermissionError("simulated Windows file lock")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)

    plan.discard_partial()

    assert plan.tmp_path.exists()
    assert not plan.etag_sidecar.exists()
    assert plan.resume_size == 0
    assert plan.partial_etag is None


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
            (download_directory / "Lecture-abcdefghijk.mp4").write_bytes(b"video")

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
        download_kind=DownloadKind.YOUTUBE,
    )
    assert node is not None
    download_directory = node_path(ctx, section)

    assert downloader.scan_and_download_youtube(ctx, node).is_handled

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
            (video_path / "Lecture-abcdefghijk.mp4").write_bytes(b"video")

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    assert downloader.scan_and_download_youtube(ctx, video_node).is_handled
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


def test_format_size_uses_binary_units():
    assert format_size(10) == "10 B"
    assert format_size(1024) == "1 KiB"
    assert format_size(1536) == "1.5 KiB"
    assert format_size(5 * 1024**2) == "5 MiB"
    assert format_size(1024**5) == "1 PiB"


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


def build_opencast_tree(etag):
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    course = semester.add_child("Opencast Course", 301, "Course")
    section = course.add_child("Recordings", 401, "Section")
    video = section.add_child(
        "Lecture (presentation).mp4",
        OPENCAST_EPISODE_ID,
        "Opencast",
        url=OPENCAST_VIDEO_URL,
        etag=etag,
        etag_kind=RemoteMarkerKind.CONTENT_HASH,
        remote_size=5,
        download_kind=DownloadKind.OPENCAST,
    )
    return root, video


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
        Node(
            "Video",
            link,
            "Youtube",
            None,
            url=link,
            download_kind=DownloadKind.YOUTUBE,
        )
    )
    url = YOUTUBE_WATCH_URL.format(video_id=video_id) if video_id is not None else link
    root = Node("", -1, "Root", None)
    semester = root.add_child("26ss", None, "Semester")
    course = semester.add_child("Video Course", 301, "Course")
    section = course.add_child("General", 401, "Section")
    video = section.add_child(
        "Video",
        video_id or link,
        "Youtube",
        url=url,
        download_kind=DownloadKind.YOUTUBE,
    )
    return root, section, video


def test_existing_youtube_download_is_marked_handled(tmp_path):
    ctx = make_context({"paths.sync_directory": str(tmp_path)})
    root, section, video = build_youtube_tree("https://youtu.be/abcdefghijk")
    video_directory = node_path(ctx, section)
    video_directory.mkdir(parents=True)
    (video_directory / "Lecture-abcdefghijk.mp4").write_bytes(b"existing video")

    downloader.download_node_tree(ctx, root)

    assert video.is_handled
    assert ctx.stats.unchanged == 1


def test_download_dispatch_uses_semantic_kind_not_display_type(monkeypatch):
    node = Node(
        "Video",
        "abcdefghijk",
        "External video",
        None,
        url="https://youtu.be/abcdefghijk",
        download_kind=DownloadKind.YOUTUBE,
    )
    dispatched = []

    def download_youtube(ctx, candidate, log):
        dispatched.append(candidate)
        return HANDLED_DOWNLOAD

    monkeypatch.setattr(downloader, "scan_and_download_youtube", download_youtube)

    assert downloader.download_leaf(
        make_context(), node, logging.getLogger()
    ).is_handled
    assert dispatched == [node]


# --------------------------------------------------------------------------
# Actual download happy path (gap 2)
# --------------------------------------------------------------------------


def test_download_streams_chunks_to_disk_and_records_metadata(tmp_path, capsys):
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

    outcome = download_file(syncer, file_node)

    assert outcome.is_handled

    assert download_path.read_bytes() == b"".join(chunks)
    # The temp part-file and its etag sidecar are cleaned up on completion.
    assert list(download_path.parent.glob(".*.smmpart*")) == []
    # mtime is aligned with Moodle's timemodified so later runs detect changes.
    assert int(download_path.stat().st_mtime) == 1710000500
    # The ETag is persisted on the node for the next run's change detection.
    assert file_node.etag == etag
    assert syncer.session.count("GET", URL) == 1
    assert outcome.downloaded == 1
    assert outcome.transferred_bytes == len(b"".join(chunks))
    output = capsys.readouterr().out
    assert f"Downloading {download_path} [{file_node.type}]" in output
    assert f"Downloaded {download_path} [{file_node.type}]" in output


@pytest.mark.parametrize("algorithm", ["md5", "sha1", "sha256"])
def test_download_rejects_wrong_advertised_checksum_at_expected_size(
    tmp_path,
    algorithm,
    capsys,
):
    expected = b"correct remote bytes"
    wrong = b"broken remote bytes!"
    assert len(wrong) == len(expected)
    config = {
        "paths.sync_directory": str(tmp_path),
        "downloads.update_files": True,
        "downloads.conflict_handling": "overwrite",
    }
    syncer, file_node = make_run_syncer(
        config,
        timemodified=1710000500,
        etag=hashlib.new(algorithm, expected, usedforsecurity=False).hexdigest(),
        remote_size=len(expected),
    )
    file_node.etag_kind = RemoteMarkerKind.CONTENT_HASH
    download_path = node_path(syncer, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(b"existing local file")
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": str(len(wrong)),
            },
            chunks=[wrong],
        ),
    )

    outcome = download_file(syncer, file_node)

    assert not outcome.is_handled
    assert download_path.read_bytes() == b"existing local file"
    assert file_node.content_hash is None
    assert list(download_path.parent.glob(".*.smmpart*")) == []
    assert (
        f"Downloaded {download_path} [{file_node.type}]" not in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("headers", "remote_size"),
    [
        ({"Content-Length": "12"}, None),
        ({}, 12),
    ],
    ids=["content-length", "known-remote-size"],
)
def test_download_rejects_short_body(tmp_path, headers, remote_size):
    syncer, file_node = make_run_syncer(
        {"paths.sync_directory": str(tmp_path)},
        timemodified=1710000500,
        remote_size=remote_size,
    )
    download_path = node_path(syncer, file_node)
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf", **headers},
            chunks=[b"short"],
        ),
    )

    assert not download_file(syncer, file_node).is_handled
    assert not download_path.exists()
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_fresh_download_rejects_nonzero_partial_response(tmp_path):
    syncer, file_node = make_run_syncer(
        {"paths.sync_directory": str(tmp_path)},
        timemodified=1710000500,
    )
    download_path = node_path(syncer, file_node)
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            status_code=206,
            headers={
                "Content-Type": "application/pdf",
                "Content-Range": "bytes 5-8/9",
                "Content-Length": "4",
            },
            chunks=[b"TAIL"],
        ),
    )

    assert not download_file(syncer, file_node).is_handled
    assert not download_path.exists()
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_fresh_download_accepts_complete_zero_based_partial_response(tmp_path):
    syncer, file_node = make_run_syncer(
        {"paths.sync_directory": str(tmp_path)},
        timemodified=1710000500,
    )
    file_node.download_headers = {"Range": "bytes=0-"}
    download_path = node_path(syncer, file_node)
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            status_code=206,
            headers={
                "Content-Type": "application/pdf",
                "Content-Range": "bytes 0-3/4",
                "Content-Length": "4",
            },
            chunks=[b"data"],
        ),
    )

    assert download_file(syncer, file_node).downloaded == 1
    assert download_path.read_bytes() == b"data"


def test_fresh_compressed_full_response_ignores_encoded_content_length(tmp_path):
    syncer, file_node = make_run_syncer(
        {"paths.sync_directory": str(tmp_path)},
        timemodified=1710000500,
    )
    download_path = node_path(syncer, file_node)
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={
                "Content-Type": "application/pdf",
                "Content-Encoding": "gzip",
                "Content-Length": "20",
            },
            chunks=[b"decoded body"],
        ),
    )

    assert download_file(syncer, file_node).downloaded == 1
    assert download_path.read_bytes() == b"decoded body"


def test_yt_dlp_progress_payload_updates_shared_progress():
    ctx = make_context()
    progress = ctx.output.transfer(total=None)

    downloader.update_yt_dlp_progress(
        progress,
        {
            "status": "downloading",
            "downloaded_bytes": 512,
            "total_bytes_estimate": 1024,
        },
    )
    downloader.update_yt_dlp_progress(
        progress,
        {
            "status": "downloading",
            "downloaded_bytes": 128,
            "total_bytes": 256,
        },
    )
    downloader.update_yt_dlp_progress(
        progress,
        {
            "status": "finished",
            "downloaded_bytes": 256,
            "total_bytes": 256,
        },
    )

    assert progress.transferred_bytes == 768


@pytest.mark.parametrize(
    ("version", "outdated"),
    [("2025.1.1", True), (YT_DLP_TESTED_VERSION, False)],
)
def test_failed_youtube_download_reports_yt_dlp_version(
    tmp_path, monkeypatch, caplog, version, outdated
):
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, urls):
            return 1

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(downloader.yt_dlp.version, "__version__", version)
    ctx = make_context({"paths.sync_directory": str(tmp_path)})
    root, _, video = build_youtube_tree("https://youtu.be/abcdefghijk")

    downloader.download_node_tree(ctx, root)

    assert video.is_handled is False
    assert ctx.stats.failed == 1
    assert captured["noprogress"] is True
    assert len(captured["progress_hooks"]) == 1
    assert isinstance(captured["logger"], downloader.YtDlpLogger)
    assert f"yt-dlp failed with installed version {version}" in caplog.text
    assert (
        f"older than the tested baseline {YT_DLP_TESTED_VERSION}" in caplog.text
    ) is outdated


def test_youtube_success_without_output_is_reported_by_tree_walk(
    tmp_path, monkeypatch, caplog
):
    class FakeYoutubeDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, urls):
            return 0

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    ctx = make_context({"paths.sync_directory": str(tmp_path)})
    root, _, video = build_youtube_tree("https://youtu.be/abcdefghijk")

    downloader.download_node_tree(ctx, root)

    assert not video.is_handled
    assert ctx.stats.failed == 1
    assert "did not download YouTube video" in caplog.text


def test_failed_install_is_not_marked_handled(tmp_path, monkeypatch):
    ctx = make_context({"paths.sync_directory": str(tmp_path)})
    ctx.session = FakeSession()
    root, file_node = build_single_file_tree("slides.pdf", URL)
    ctx.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf"},
            chunks=[b"downloaded bytes"],
        ),
    )
    monkeypatch.setattr(
        downloader,
        "install_downloaded_file",
        lambda *args, **kwargs: False,
    )

    downloader.download_node_tree(ctx, root)

    assert not file_node.is_handled
    assert ctx.stats.failed == 1
    assert ctx.stats.downloaded == 0


def test_download_walk_reports_progress_for_every_pending_item(monkeypatch, capsys):
    ctx = make_context()
    root = Node("", -1, "Root", None)
    section = root.add_child("Course", 1, "Section")
    first = section.add_child("slides.pdf", 2, "File", url="https://example.test/1")
    second = section.add_child("lecture.mp4", 3, "Video", url="https://example.test/2")
    handled = section.add_child("old.pdf", 4, "File", url="https://example.test/3")
    handled.mark_handled()
    visited = []
    monkeypatch.setattr(
        downloader,
        "download_leaf",
        lambda context, node, log: visited.append(node) or HANDLED_DOWNLOAD,
    )

    downloader.download_node_tree(ctx, root)

    assert visited == [first, second]
    assert first.is_handled
    assert second.is_handled
    output = capsys.readouterr().out
    assert "Processing 2 items..." in output
    assert "[1/2] Processing File: Course/slides.pdf" in output
    assert "[2/2] Processing Video: Course/lecture.mp4" in output


def test_download_is_skipped_for_excluded_filetypes(tmp_path):
    config = {
        "paths.sync_directory": str(tmp_path),
        "filters.exclude_filetypes": ["pdf"],
    }
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = node_path(syncer, file_node)

    # No GET route registered: a request would raise in the fake session.
    assert download_file(syncer, file_node).is_handled
    assert not download_path.exists()
    assert syncer.session.calls == []
    assert {
        (item.config_key, item.item, item.reason) for item in syncer.filtered_items
    } == {
        (
            "filters.exclude_filetypes",
            str(download_path),
            "extension 'pdf' is excluded",
        )
    }


def test_excluded_filetypes_are_case_insensitive_and_accept_a_leading_dot(tmp_path):
    syncer = make_context(
        {
            "paths.sync_directory": str(tmp_path),
            "filters.exclude_filetypes": [".pDf"],
        }
    )
    node = Node("SLIDES.PDF", 1, "File", None, url=URL)

    assert (
        downloader.should_skip_before_decision(
            syncer,
            node,
            tmp_path / node.name,
        )
        is not None
    )
    assert {item.reason for item in syncer.filtered_items} == {
        "extension 'pdf' is excluded"
    }


def test_extensionless_name_is_not_treated_as_a_filetype(tmp_path):
    syncer = make_context({"filters.exclude_filetypes": ["README"]})
    node = Node("README", 1, "File", None, url="https://example.test/README")

    assert (
        downloader.should_skip_before_decision(syncer, node, tmp_path / node.name)
        is None
    )
    assert syncer.filtered_items == set()


def test_download_is_skipped_for_excluded_filename_pattern(tmp_path):
    config = {
        "paths.sync_directory": str(tmp_path),
        "filters.exclude_files": ["slide*.pdf"],
    }
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = node_path(syncer, file_node)

    assert download_file(syncer, file_node).is_handled
    assert not download_path.exists()
    assert {
        (item.config_key, item.item, item.reason) for item in syncer.filtered_items
    } == {
        (
            "filters.exclude_files",
            str(download_path),
            "matches 'slide*.pdf'",
        )
    }


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
    assert download_file(syncer, file_node).is_handled
    assert not download_path.exists()
    assert file_node.remote_size == 2048
    assert caplog.messages == []
    assert {
        (item.config_key, item.item, item.reason) for item in syncer.filtered_items
    } == {
        (
            "filters.max_file_size",
            str(download_path),
            "size (2 KiB) exceeds the configured limit (1 KiB)",
        )
    }


def test_download_uses_known_remote_size_before_get(tmp_path):
    config = {"paths.sync_directory": str(tmp_path), "filters.max_file_size": "1K"}
    syncer, file_node = make_run_syncer(
        config, timemodified=1710000500, remote_size=2048
    )
    download_path = node_path(syncer, file_node)

    # No GET route registered: the known size is enough to skip.
    assert download_file(syncer, file_node).is_handled
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

    assert download_file(syncer, file_node).is_handled
    assert not download_path.exists()
    assert file_node.remote_size == 10
    assert {(item.config_key, item.reason) for item in syncer.filtered_items} == {
        (
            "filters.min_file_size",
            "size (10 B) is below the configured limit (1 KiB)",
        )
    }


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

    assert downloader.scan_and_download_youtube(syncer, video_node).is_handled
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

    assert downloader.scan_and_download_youtube(syncer, video_node).is_handled
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

    assert downloader.scan_and_download_youtube(syncer, video_node).is_handled
    assert "Would download" not in capsys.readouterr().out
    assert not node_path(syncer, section).exists()
    assert video_node.remote_size == 5 * 1024**2
    assert {item.config_key for item in syncer.filtered_items} == {
        "filters.max_file_size"
    }


def test_yt_dlp_estimated_size_sums_requested_formats():
    assert downloader.yt_dlp_estimated_size({"filesize": 100}) == 100
    assert downloader.yt_dlp_estimated_size({"filesize_approx": 200}) == 200
    assert (
        downloader.yt_dlp_estimated_size(
            {"requested_formats": [{"filesize": 100}, {"filesize_approx": 50}]}
        )
        == 150
    )
    # Unknown sizes must not trigger the limit.
    assert downloader.yt_dlp_estimated_size(None) is None
    assert downloader.yt_dlp_estimated_size({}) is None
    assert (
        downloader.yt_dlp_estimated_size({"requested_formats": [{"filesize": 100}, {}]})
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

    assert download_file(syncer, file_node).is_handled
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

    assert download_file(syncer, file_node).is_handled
    assert node_path(syncer, file_node).read_bytes() == b"data"


def test_repeated_download_503_opens_origin_circuit(caplog, tmp_path):
    syncer, file_node = make_run_syncer(
        {"paths.sync_directory": str(tmp_path)},
        timemodified=1710000500,
    )
    syncer.session.add("GET", URL, FakeResponse(status_code=503))
    caplog.set_level(logging.WARNING, logger="syncmymoodle.downloader")

    for _ in range(4):
        assert not download_file(syncer, file_node).is_handled

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


def test_download_rejects_redirect_outside_allowed_domains(tmp_path, caplog):
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
    caplog.set_level(logging.WARNING, logger="syncmymoodle.downloader")

    assert not download_file(syncer, file_node).is_handled
    assert syncer.session.calls == [("GET", URL)]
    assert not node_path(syncer, file_node).exists()
    assert caplog.messages == []
    assert {
        (item.config_key, item.item, item.reason) for item in syncer.filtered_items
    } == {
        (
            "filters.allowed_domains",
            f"redirected Linked file [application/pdf] file: {external_url}",
            "host 'files.example.test' is not allowed",
        )
    }


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

    assert download_file(syncer, file_node).is_handled
    assert node_path(syncer, file_node).read_bytes() == b"data"


def test_dry_run_reports_downloads_without_writing(tmp_path, capsys):
    config = {"paths.sync_directory": str(tmp_path), "downloads.dry_run": True}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = node_path(syncer, file_node)

    # No GET route registered: any request would raise in the fake session.
    assert download_file(syncer, file_node).is_handled
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

    assert download_file(syncer, file_node).is_handled
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
    assert section is not None
    second_node = section.add_child(
        "dup.pdf", URL + "?v=2", "Linked file [application/pdf]", url=URL
    )

    assert download_file(syncer, first_node).is_handled
    assert download_file(syncer, second_node).is_handled
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
    assert download_file(syncer, file_node).is_handled
    assert download_path.read_bytes() == local_modified
    assert syncer.session.calls == []


def test_conflict_overwrite_replaces_local_file(tmp_path):
    syncer, file_node, download_path, _ = _setup_conflict(tmp_path, "overwrite")
    new_body = _add_new_remote(syncer)

    assert download_file(syncer, file_node).is_handled
    assert download_path.read_bytes() == new_body
    # Overwrite mode leaves no side-car conflict copy behind.
    assert list(download_path.parent.glob("*.syncconflict.*")) == []
    assert syncer.session.count("GET", URL) == 1


def test_conflict_rename_moves_local_file_aside_before_download(tmp_path):
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "rename"
    )
    new_body = _add_new_remote(syncer)

    assert download_file(syncer, file_node).is_handled

    # The fresh remote content lands at the canonical path.
    assert download_path.read_bytes() == new_body
    # The user's local edits are preserved in a side-car conflict file.
    conflicts = list(download_path.parent.glob("*.syncconflict.*"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == local_modified
    assert syncer.session.count("GET", URL) == 1


def test_identical_staged_update_does_not_create_conflict_copy(tmp_path):
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "rename"
    )
    _add_new_remote(syncer, local_modified)
    root = file_node
    while root.parent is not None:
        root = root.parent
    syncer.root_node = root

    downloader.download_node_tree(syncer, root)
    course_cache.cache_root_node(syncer)

    assert syncer.stats.unchanged == 1
    assert syncer.stats.updated == 0
    assert syncer.stats.transferred_bytes == len(local_modified)
    assert download_path.read_bytes() == local_modified
    assert file_node.content_hash == sha256(local_modified)
    assert int(download_path.stat().st_mtime) == 1710000400
    assert download_path in syncer.downloaded_paths
    assert list(download_path.parent.glob("*.syncconflict.*")) == []
    assert not (download_path.parent / f".{download_path.name}.smmpart").exists()
    loaded = make_context({"paths.sync_directory": str(tmp_path)})
    cached_file = course_cache.get_old_node_for(loaded, file_node)
    assert cached_file is not None
    assert cached_file.timemodified == 1710000400
    assert cached_file.content_hash == sha256(local_modified)


def test_identical_staged_update_is_reported_as_unchanged(tmp_path, capsys):
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "rename"
    )
    _add_new_remote(syncer, local_modified)

    outcome = download_file(syncer, file_node)

    assert outcome.unchanged == 1
    assert outcome.downloaded == 0
    output = capsys.readouterr().out
    assert f"Unchanged {download_path} [{file_node.type}]" in output
    assert f"Downloaded {download_path} [{file_node.type}]" not in output


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

    assert download_file(syncer, file_node).is_handled
    assert syncer.session.calls == []
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


@pytest.mark.parametrize("has_cached_hash", [False, True])
def test_changed_content_hash_wins_over_equal_timemodified(tmp_path, has_cached_hash):
    original = b"original remote content"
    updated = b"updated remote content"
    config = {
        "paths.sync_directory": str(tmp_path),
        "downloads.update_files": True,
    }
    seeded = make_context(config)
    seeded.root_node, seeded_file = build_single_file_tree(
        "slides.pdf",
        URL,
        timemodified=1710000300,
        etag=sha1(original) if has_cached_hash else None,
    )
    if has_cached_hash:
        seeded_file.etag_kind = RemoteMarkerKind.CONTENT_HASH
    seeded_file.mark_handled()
    course_cache.cache_root_node(seeded)

    current, current_file = make_run_syncer(
        config,
        timemodified=1710000300,
        etag=sha1(updated),
    )
    current_file.etag_kind = RemoteMarkerKind.CONTENT_HASH
    download_path = node_path(current, current_file)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(original)
    os.utime(download_path, (1710000300, 1710000300))
    current.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf"},
            chunks=[updated],
        ),
    )

    assert download_file(current, current_file).updated == 1
    assert download_path.read_bytes() == updated
    assert current.session.calls == [("GET", URL)]


@pytest.mark.parametrize(
    ("conflict_mode", "expected_target", "expected_conflict"),
    [
        ("keep", b"edit made during transfer", None),
        ("rename", b"updated remote content", b"edit made during transfer"),
        ("overwrite", b"updated remote content", None),
    ],
)
def test_target_changed_during_transfer_obeys_conflict_policy(
    tmp_path,
    conflict_mode,
    expected_target,
    expected_conflict,
    capsys,
):
    original = b"original remote content"
    updated = b"updated remote content"
    config = {
        "paths.sync_directory": str(tmp_path),
        "downloads.update_files": True,
        "downloads.conflict_handling": conflict_mode,
    }
    seed_course_cache(config, timemodified=100, etag=sha1(original))
    current, current_file = make_run_syncer(config, timemodified=200)
    download_path = node_path(current, current_file)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(original)

    def edit_while_request_is_in_flight(url, kwargs):
        download_path.write_bytes(b"edit made during transfer")
        return FakeResponse(
            headers={"Content-Type": "application/pdf"},
            chunks=[updated],
        )

    current.session.add("GET", URL, edit_while_request_is_in_flight)

    outcome = download_file(current, current_file)

    assert outcome.is_handled
    assert download_path.read_bytes() == expected_target
    conflicts = list(download_path.parent.glob("*.syncconflict.*"))
    if expected_conflict is None:
        assert conflicts == []
    else:
        assert len(conflicts) == 1
        assert conflicts[0].read_bytes() == expected_conflict
    completed_line = f"Downloaded {download_path} [{current_file.type}]"
    assert (completed_line in capsys.readouterr().out) is (conflict_mode != "keep")


def test_legacy_course_cache_prevents_unchanged_file_false_conflict(tmp_path):
    config = {
        "paths.sync_directory": str(tmp_path),
        "downloads.update_files": True,
        "downloads.conflict_handling": "rename",
    }
    seeded = make_context(config)
    seeded.root_node, seeded_file = build_single_file_tree(
        "slides.pdf", URL, timemodified=1710000300
    )
    seeded_file.mark_handled()
    course_node = seeded.root_node.children[0].children[0]
    course_cache.cache_root_node(seeded)
    stable_path = course_cache.course_cache_path(seeded, course_node)
    payload = read_private_gzip_json(stable_path, "course cache")
    assert isinstance(payload, dict)
    payload["format"] = course_cache.LEGACY_COURSE_CACHE_FORMAT
    payload.pop("identity")
    legacy_path = node_path(seeded, course_node) / COURSE_CACHE_FILENAME
    write_private_gzip_json(legacy_path, payload)
    stable_path.unlink()

    current, current_file = make_run_syncer(config, timemodified=1710000300)
    download_path = node_path(current, current_file)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(b"unchanged remote bytes")
    os.utime(download_path, (1710000300, 1710000300))

    outcome = download_file(current, current_file)

    assert outcome.unchanged == 1
    assert current.session.calls == []
    assert download_path.read_bytes() == b"unchanged remote bytes"
    assert list(download_path.parent.glob("*.syncconflict.*")) == []
    assert stable_path.exists()
    assert not legacy_path.exists()


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
    os.utime(download_path, (1710000300, 1710000300))
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf"}, chunks=[b"NEW CORRECT VERSION"]
        ),
    )

    assert download_file(syncer, file_node).is_handled
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

    assert download_file(syncer, file_node).is_handled
    assert syncer.session.calls == []
    assert download_path.read_bytes() == content


def test_unchanged_opencast_file_does_not_authorize(tmp_path, monkeypatch):
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    marker = "11111111111111111111111111111111"
    cached = make_context(config)
    cached.root_node, cached_video = build_opencast_tree(marker)
    cached_video.mark_handled()
    course_cache.cache_root_node(cached)

    current = make_context(config)
    current.session = FakeSession()
    current.root_node, video = build_opencast_tree(marker)
    download_path = node_path(current, video)
    download_path.parent.mkdir(parents=True)
    download_path.write_bytes(b"video")
    monkeypatch.setattr(
        opencast,
        "authorize_course_for_episode",
        lambda *args, **kwargs: pytest.fail("unchanged video was authorized"),
    )

    outcome = download_file(current, video)

    assert outcome.unchanged == 1
    assert current.session.calls == []


def test_stale_opencast_metadata_cannot_replace_existing_file(tmp_path, monkeypatch):
    marker = "11111111111111111111111111111111"
    current = make_context(
        {
            "paths.sync_directory": str(tmp_path),
            "downloads.update_files": True,
        }
    )
    current.session = FakeSession()
    current.root_node, video = build_opencast_tree(marker)
    opencast.store_episode(
        current,
        301,
        OPENCAST_EPISODE_ID,
        opencast.OpencastEpisode(
            (
                opencast.OpencastTrack(
                    OPENCAST_VIDEO_URL,
                    checksum_type="md5",
                    checksum=marker,
                    flavor_type="presentation",
                ),
            )
        ),
        state=None,
    )
    monkeypatch.setattr(
        opencast,
        "authorize_course_for_episode",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        opencast,
        "fetch_result_list",
        lambda *args, **kwargs: [{"mediapackage": {"id": OPENCAST_EPISODE_ID}}],
    )
    assert (
        opencast.resolve_tracks_from_episode(
            current,
            OPENCAST_EPISODE_ID,
            course_id=301,
        )
        is not None
    )
    download_path = node_path(current, video)
    download_path.parent.mkdir(parents=True)
    download_path.write_bytes(b"existing video")

    outcome = download_file(current, video)

    assert not outcome.is_handled
    assert current.session.calls == []
    assert download_path.read_bytes() == b"existing video"


def test_missing_opencast_file_authorizes_immediately_before_download(
    tmp_path,
    monkeypatch,
):
    current = make_context({"paths.sync_directory": str(tmp_path)})
    current.session = FakeSession()
    current.root_node, video = build_opencast_tree(
        hashlib.md5(b"video", usedforsecurity=False).hexdigest()
    )
    video.type = "Lecture recording"
    authorized = []
    monkeypatch.setattr(
        opencast,
        "authorize_course_for_episode",
        lambda ctx, course_id, episode_id, log: (
            authorized.append((course_id, episode_id)) or True
        ),
    )
    current.session.add(
        "GET",
        OPENCAST_VIDEO_URL,
        FakeResponse(
            headers={"Content-Type": "video/mp4", "Content-Length": "5"},
            chunks=[b"video"],
        ),
    )

    outcome = download_file(current, video)

    assert outcome.downloaded == 1
    assert authorized == [(301, OPENCAST_EPISODE_ID)]
    assert node_path(current, video).read_bytes() == b"video"


def test_checksumless_opencast_validation_authorizes_before_conditional_get(
    tmp_path,
    monkeypatch,
):
    config = {"paths.sync_directory": str(tmp_path), "downloads.update_files": True}
    cached = make_context(config)
    cached.root_node, cached_video = build_opencast_tree('"video-v1"')
    cached_video.etag_kind = RemoteMarkerKind.OPAQUE
    cached_video.mark_handled()
    course_cache.cache_root_node(cached)

    current = make_context(config)
    current.session = FakeSession()
    current.root_node, video = build_opencast_tree(None)
    download_path = node_path(current, video)
    download_path.parent.mkdir(parents=True)
    download_path.write_bytes(b"video")
    authorized = []
    monkeypatch.setattr(
        opencast,
        "authorize_course_for_episode",
        lambda ctx, course_id, episode_id, log: (
            authorized.append((course_id, episode_id)) or True
        ),
    )
    current.session.add(
        "GET",
        OPENCAST_VIDEO_URL,
        FakeResponse(status_code=304, headers={"ETag": '"video-v1"'}),
    )

    outcome = download_file(current, video)

    assert outcome.unchanged == 1
    assert authorized == [(301, OPENCAST_EPISODE_ID)]
    assert current.session.count("GET", OPENCAST_VIDEO_URL) == 1


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

    assert download_file(syncer, file_node).is_handled
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

    assert download_file(syncer, file_node).is_handled
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

    assert download_file(syncer, file_node).is_handled
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

    assert download_file(syncer, current_second).is_handled
    assert syncer.session.calls == []
    assert list(download_path.parent.glob("*.syncconflict.*")) == []
    assert download_path.read_bytes() == content


def test_distinct_files_in_merged_sections_are_both_downloaded(tmp_path):
    syncer = make_context({"paths.sync_directory": str(tmp_path)})
    syncer.session = FakeSession()
    root, first_file, second_file = build_duplicate_section_file_tree()
    pathing.resolve_node_path_clashes(root)
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
    assert download_file(syncer, first_file).is_handled
    assert download_file(syncer, second_file).is_handled
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

    def unverifiable(path, marker, snapshot=None):
        del path, marker, snapshot
        return downloader.FileMatch.UNKNOWN

    monkeypatch.setattr("syncmymoodle.downloader.classify_local_file", unverifiable)

    assert download_file(syncer, file_node).is_handled
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

    def unverifiable(path, marker, snapshot=None):
        del path, marker, snapshot
        return downloader.FileMatch.UNKNOWN

    monkeypatch.setattr("syncmymoodle.downloader.classify_local_file", unverifiable)

    assert download_file(syncer, file_node).is_handled
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

    assert not download_file(syncer, file_node).is_handled
    assert download_path.exists()
    assert download_path.read_bytes() == local_modified
    assert list(download_path.parent.glob("*.syncconflict.*")) == []


def test_rename_conflict_non_2xx_update_preserves_canonical_file(tmp_path):
    syncer, file_node, download_path, local_modified = _setup_conflict(
        tmp_path, "rename"
    )
    syncer.session.add("GET", URL, FakeResponse(status_code=403, text="forbidden"))

    assert not download_file(syncer, file_node).is_handled
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
    assert download_file(syncer, file_node).is_handled
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

    assert download_file(syncer, file_node).is_handled
    # The partial head is kept and the resumed tail appended.
    assert download_path.read_bytes() == b"HEAD-TAIL"
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_resume_rejects_wrong_checksum_and_discards_partial(tmp_path):
    expected = b"HEAD-CORRECT"
    actual = b"HEAD-INCORRT"
    assert len(actual) == len(expected)
    config = {"paths.sync_directory": str(tmp_path)}
    syncer, file_node = make_run_syncer(
        config,
        timemodified=1710000500,
        etag=sha1(expected),
        remote_size=len(expected),
    )
    file_node.etag_kind = RemoteMarkerKind.CONTENT_HASH
    download_path = _seed_partial(syncer, file_node, b"HEAD-", '"v1"')
    syncer.session.add(
        "GET",
        URL,
        FakeResponse(
            status_code=206,
            headers={
                "Content-Type": "application/pdf",
                "Content-Range": f"bytes 5-{len(actual) - 1}/{len(actual)}",
                "Content-Length": str(len(actual) - 5),
                "ETag": '"v1"',
            },
            chunks=[actual[5:]],
        ),
    )

    assert not download_file(syncer, file_node).is_handled
    assert not download_path.exists()
    assert file_node.content_hash is None
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_resume_rejects_short_range_body(tmp_path):
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
                "Content-Length": "4",
                "ETag": '"v1"',
            },
            chunks=[b"TA"],
        ),
    )

    assert not download_file(syncer, file_node).is_handled
    assert not download_path.exists()
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_resume_rejects_encoded_partial_response(tmp_path):
    config = {"paths.sync_directory": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = _seed_partial(syncer, file_node, b"HEAD-", '"v1"')

    def encoded_response(url, kwargs):
        assert kwargs["headers"]["Accept-Encoding"] == "identity"
        return FakeResponse(
            status_code=206,
            headers={
                "Content-Type": "application/pdf",
                "Content-Encoding": "gzip",
                "Content-Range": "bytes 5-8/9",
                "Content-Length": "4",
                "ETag": '"v1"',
            },
            chunks=[b"TAIL"],
        )

    syncer.session.add("GET", URL, encoded_response)

    assert not download_file(syncer, file_node).is_handled
    assert not download_path.exists()
    assert list(download_path.parent.glob(".*.smmpart*")) == []


def test_resume_rejects_content_range_with_unknown_total(tmp_path):
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
                "Content-Range": "bytes 5-8/*",
                "Content-Length": "4",
                "ETag": '"v1"',
            },
            chunks=[b"TAIL"],
        ),
    )

    assert not download_file(syncer, file_node).is_handled
    assert not download_path.exists()
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

    assert not download_file(syncer, file_node).is_handled
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

    assert download_file(syncer, file_node).is_handled
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

    assert not download_file(syncer, file_node).is_handled
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

    assert download_file(syncer, file_node).is_handled
    assert download_path.read_bytes() == b"FRESH"
    assert list(download_path.parent.glob(".*.smmpart*")) == []


@pytest.mark.parametrize(
    "saved_etag",
    ['W/"v1"', "v1", '"bad etag"'],
    ids=["weak", "unquoted", "invalid-character"],
)
def test_partial_with_unusable_etag_is_not_resumed(tmp_path, saved_etag):
    config = {"paths.sync_directory": str(tmp_path)}
    syncer, file_node = make_run_syncer(config, timemodified=1710000500)
    download_path = _seed_partial(syncer, file_node, b"STALE", saved_etag)

    def full_response(url, kwargs):
        assert "Range" not in kwargs["headers"]
        assert "If-Range" not in kwargs["headers"]
        return FakeResponse(
            headers={"Content-Type": "application/pdf", "ETag": '"v2"'},
            chunks=[b"FRESH"],
        )

    syncer.session.add("GET", URL, full_response)

    assert download_file(syncer, file_node).downloaded == 1
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


def _duplicate_sciebo_destinations(
    first_url,
    second_url,
    first_etag,
    second_etag=None,
):
    root, first = _sciebo_tree(
        first_etag,
        etag_kind=RemoteMarkerKind.OPAQUE,
    )
    first.url = first_url
    semester = root.children[0]
    second_course = semester.add_child("Other Course", 302, "Course")
    second_section = second_course.add_child("General", 402, "Section")
    second = second_section.add_download_child(
        "notes.pdf",
        None,
        "Sciebo File",
        url=second_url,
        download_headers={"Authorization": "Basic x"},
        etag=second_etag or first_etag,
        etag_kind=RemoteMarkerKind.OPAQUE,
    )
    return root, first, second


def test_duplicate_sciebo_destinations_reuse_one_verified_transfer(tmp_path):
    content = b"shared sciebo notes"
    first_url = SCIEBO_URL + "?X-Amz-Date=20260716T100000Z&sig=first-secret"
    second_url = SCIEBO_URL + "?sig=second-secret&X-Amz-Date=20260716T110000Z"
    _, first, second = _duplicate_sciebo_destinations(
        first_url,
        second_url,
        '"revision-1"',
    )
    syncer = make_context({"paths.sync_directory": str(tmp_path)})
    syncer.session = FakeSession()
    second_path = node_path(syncer, second)
    second_path.parent.mkdir(parents=True)
    for suffix in ("smmpart", "smmpart.etag"):
        (second_path.parent / f".{second_path.name}.{suffix}").write_bytes(b"stale")
    syncer.session.add(
        "GET",
        first_url,
        FakeResponse(
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": str(len(content)),
            },
            chunks=[content],
        ),
    )

    first_outcome = download_file(syncer, first)
    second_outcome = download_file(syncer, second)

    assert first_outcome.downloaded == 1
    assert first_outcome.transferred_bytes == len(content)
    assert second_outcome.downloaded == 1
    assert second_outcome.transferred_bytes == 0
    assert syncer.session.count("GET") == 1
    assert node_path(syncer, first).read_bytes() == content
    assert second_path.read_bytes() == content
    assert first.content_hash == second.content_hash == sha256(content)
    assert not list(second_path.parent.glob(".*.smm*"))


def test_reuse_succeeds_when_a_stale_partial_is_windows_locked(
    tmp_path,
    monkeypatch,
):
    content = b"shared sciebo notes"
    _, first, second = _duplicate_sciebo_destinations(
        SCIEBO_URL,
        SCIEBO_URL,
        '"revision-1"',
    )
    syncer = make_context({"paths.sync_directory": str(tmp_path)})
    syncer.session = FakeSession()
    syncer.session.add(
        "GET",
        SCIEBO_URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[content]),
    )
    assert download_file(syncer, first).is_handled

    second_path = node_path(syncer, second)
    second_path.parent.mkdir(parents=True)
    partial = second_path.parent / f".{second_path.name}.smmpart"
    sidecar = partial.with_name(partial.name + ".etag")
    partial.write_bytes(b"stale partial")
    sidecar.write_text('"stale-revision"', encoding="utf-8")
    original_unlink = Path.unlink

    def locked_unlink(path, *args, **kwargs):
        if path == partial:
            raise PermissionError("simulated Windows file lock")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)

    assert download_file(syncer, second).downloaded == 1

    assert syncer.session.count("GET", SCIEBO_URL) == 1
    assert second_path.read_bytes() == content
    assert partial.read_bytes() == b"stale partial"
    assert not sidecar.exists()
    assert not list(second_path.parent.glob(".*.smmreuse*"))


def test_reused_transfer_preserves_a_target_created_while_staging(
    tmp_path,
    monkeypatch,
):
    content = b"shared sciebo notes"
    local_edit = b"created while staging"
    _, first, second = _duplicate_sciebo_destinations(
        SCIEBO_URL,
        SCIEBO_URL,
        '"revision-1"',
    )
    syncer = make_context(
        {
            "paths.sync_directory": str(tmp_path),
            "downloads.conflict_handling": "keep",
        }
    )
    syncer.session = FakeSession()
    syncer.session.add(
        "GET",
        SCIEBO_URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[content]),
    )
    assert download_file(syncer, first).is_handled

    second_path = node_path(syncer, second)
    original_copyfile = downloader.shutil.copyfile

    def create_target_while_staging(source, destination):
        result = original_copyfile(source, destination)
        second_path.parent.mkdir(parents=True, exist_ok=True)
        second_path.write_bytes(local_edit)
        return result

    monkeypatch.setattr(downloader.shutil, "copyfile", create_target_while_staging)

    outcome = download_file(syncer, second)

    assert outcome.unchanged == 1
    assert not outcome.cache_verified
    assert syncer.session.count("GET", SCIEBO_URL) == 1
    assert second_path.read_bytes() == local_edit
    assert not list(second_path.parent.glob(".*.smmreuse*"))


def test_duplicate_sciebo_url_does_not_cross_authorization_scopes(tmp_path):
    content = b"shared sciebo notes"
    _, first, second = _duplicate_sciebo_destinations(
        SCIEBO_URL,
        SCIEBO_URL,
        '"revision-1"',
    )
    first.download_headers = {
        "Authorization": "Basic first-secret",
        "requesttoken": "first-request-token",
    }
    second.download_headers = {
        "Authorization": "Basic second-secret",
        "requesttoken": "second-request-token",
    }
    syncer = make_context({"paths.sync_directory": str(tmp_path)})
    syncer.session = FakeSession()
    syncer.session.add(
        "GET",
        SCIEBO_URL,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[content]),
    )

    assert download_file(syncer, first).is_handled
    assert download_file(syncer, second).is_handled

    assert syncer.session.count("GET", SCIEBO_URL) == 2
    assert len(syncer.verified_download_artifacts) == 2
    cached_keys = repr(tuple(syncer.verified_download_artifacts))
    assert "first-secret" not in cached_keys
    assert "second-secret" not in cached_keys
    assert "request-token" not in cached_keys


def test_duplicate_transfer_falls_back_to_get_if_source_was_edited(tmp_path):
    content = b"shared sciebo notes"
    first_url = SCIEBO_URL + "?sig=first-secret"
    second_url = SCIEBO_URL + "?sig=second-secret"
    _, first, second = _duplicate_sciebo_destinations(
        first_url,
        second_url,
        '"revision-1"',
    )
    syncer = make_context({"paths.sync_directory": str(tmp_path)})
    syncer.session = FakeSession()
    for url in (first_url, second_url):
        syncer.session.add(
            "GET",
            url,
            FakeResponse(
                headers={"Content-Type": "application/pdf"},
                chunks=[content],
            ),
        )

    assert download_file(syncer, first).is_handled
    first_path = node_path(syncer, first)
    local_edit = b"x" * len(content)
    first_path.write_bytes(local_edit)
    assert download_file(syncer, second).is_handled

    assert syncer.session.count("GET") == 2
    assert first_path.read_bytes() == local_edit
    assert node_path(syncer, second).read_bytes() == content
    assert not list(tmp_path.rglob("*.smmreuse*"))


def test_duplicate_url_with_different_remote_marker_is_downloaded_again(tmp_path):
    first_url = SCIEBO_URL + "?sig=first-secret"
    second_url = SCIEBO_URL + "?sig=second-secret"
    _, first, second = _duplicate_sciebo_destinations(
        first_url,
        second_url,
        '"revision-1"',
        '"revision-2"',
    )
    syncer = make_context({"paths.sync_directory": str(tmp_path)})
    syncer.session = FakeSession()
    syncer.session.add(
        "GET",
        first_url,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[b"v1"]),
    )
    syncer.session.add(
        "GET",
        second_url,
        FakeResponse(headers={"Content-Type": "application/pdf"}, chunks=[b"v2"]),
    )

    assert download_file(syncer, first).is_handled
    assert download_file(syncer, second).is_handled

    assert syncer.session.count("GET") == 2
    assert node_path(syncer, first).read_bytes() == b"v1"
    assert node_path(syncer, second).read_bytes() == b"v2"


def test_reused_transfer_preserves_rename_conflict_policy(tmp_path):
    content = b"shared sciebo notes"
    first_url = SCIEBO_URL + "?sig=first-secret"
    second_url = SCIEBO_URL + "?sig=second-secret"
    _, first, second = _duplicate_sciebo_destinations(
        first_url,
        second_url,
        '"revision-1"',
    )
    syncer = make_context(
        {
            "paths.sync_directory": str(tmp_path),
            "downloads.update_files": True,
            "downloads.conflict_handling": "rename",
        }
    )
    syncer.session = FakeSession()
    syncer.session.add(
        "GET",
        first_url,
        FakeResponse(
            headers={"Content-Type": "application/pdf"},
            chunks=[content],
        ),
    )
    second_path = node_path(syncer, second)
    second_path.parent.mkdir(parents=True)
    second_path.write_bytes(b"local edit")

    assert download_file(syncer, first).is_handled
    assert download_file(syncer, second).updated == 1

    assert syncer.session.count("GET") == 1
    assert second_path.read_bytes() == content
    conflicts = list(second_path.parent.glob("*.syncconflict.*"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == b"local edit"


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

    assert download_file(syncer, current).is_handled
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

    assert download_file(syncer, current).is_handled
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

    assert download_file(syncer, current).is_handled
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

    assert download_file(syncer, current).is_handled
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

    assert download_file(syncer, current).is_handled
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

    assert download_file(syncer, current).is_handled
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

    assert download_file(syncer, current).is_handled
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


def test_update_disabled_does_not_verify_an_untracked_existing_file(tmp_path):
    remote = b"server version"
    local = b"untracked local version"
    initial_config = {
        "paths.sync_directory": str(tmp_path),
        "downloads.update_files": False,
    }
    initial = make_context(initial_config)
    initial.session = FakeSession()
    root, file_node = build_single_file_tree(
        "slides.pdf",
        URL,
        timemodified=100,
        etag=sha1(remote),
    )
    file_node.etag_kind = RemoteMarkerKind.CONTENT_HASH
    initial.root_node = root
    download_path = node_path(initial, file_node)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(local)

    downloader.download_node_tree(initial, root)
    course_cache.cache_root_node(initial)

    cached_file = course_cache.get_old_node_for(initial, file_node)
    assert cached_file is not None
    assert cached_file.download_status is DownloadStatus.SKIPPED
    assert cached_file.etag is None

    update_config = {
        "paths.sync_directory": str(tmp_path),
        "downloads.update_files": True,
    }
    current = make_context(update_config)
    current.session = FakeSession()
    current.root_node, current_file = build_single_file_tree(
        "slides.pdf",
        URL,
        timemodified=100,
        etag=sha1(remote),
    )
    current_file.etag_kind = RemoteMarkerKind.CONTENT_HASH
    current.session.add(
        "GET",
        URL,
        FakeResponse(
            headers={"Content-Type": "application/pdf"},
            chunks=[remote],
        ),
    )

    assert download_file(current, current_file).updated == 1
    assert download_path.read_bytes() == remote
    conflicts = list(download_path.parent.glob("*.syncconflict.*"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == local


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
