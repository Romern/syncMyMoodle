import gzip
import json
import os
import stat
from types import SimpleNamespace

import pytest

from syncmymoodle import course_cache
from syncmymoodle.node import Node
from syncmymoodle.pathing import (
    make_conflict_path,
    sanitize_path_part,
    windows_extended_length_path,
)
from syncmymoodle.storage import (
    chmod_private_best_effort,
    read_private_gzip_json,
    write_private_gzip_json,
)

from .helpers import FakeSession, download_file, make_context, node_path


def test_sanitized_node_path_stays_inside_basedir(tmp_path):
    syncer = make_context({"basedir": str(tmp_path)})
    root = Node("", -1, "Root", None)
    bad_node = root.add_child("%2e%2e", 1, "Section")

    target_path = node_path(syncer, bad_node)

    assert target_path == tmp_path / "_"
    assert target_path.resolve(strict=False).is_relative_to(tmp_path)


def test_sanitize_path_part_avoids_windows_reserved_names():
    assert sanitize_path_part("...") == "_"
    assert sanitize_path_part(". . .") == "_"
    assert sanitize_path_part("Chapter 3 .") == "Chapter 3"
    assert sanitize_path_part("notes ..") == "notes"
    assert sanitize_path_part("CON") == "_CON"
    assert sanitize_path_part("CON .txt") == "_CON .txt"
    assert sanitize_path_part("aux.txt") == "_aux.txt"
    assert sanitize_path_part("COM²") == "_COM²"
    assert sanitize_path_part("lecture.") == "lecture"
    assert sanitize_path_part("bad\x00name") == "badname"


def test_empty_child_node_name_materializes_as_placeholder(tmp_path):
    syncer = make_context({"basedir": str(tmp_path)})
    root = Node("", -1, "Root", None)
    child = root.add_child("", 1, "Section")

    assert child is not None
    assert node_path(syncer, child) == tmp_path / "_"


def test_windows_extended_length_path_formats_drive_and_unc_paths():
    assert (
        windows_extended_length_path(r"C:\Moodle\Course\file.pdf")
        == r"\\?\C:\Moodle\Course\file.pdf"
    )
    assert (
        windows_extended_length_path(r"\\server\share\file.pdf")
        == r"\\?\UNC\server\share\file.pdf"
    )
    assert (
        windows_extended_length_path(r"\\?\C:\Moodle\Course\file.pdf")
        == r"\\?\C:\Moodle\Course\file.pdf"
    )


def test_conflict_path_applies_windows_prefix_after_suffix(tmp_path, monkeypatch):
    target = tmp_path / "file.pdf"
    target.write_bytes(b"content")
    monkeypatch.setattr("syncmymoodle.pathing.is_windows", lambda: True)
    monkeypatch.setattr("syncmymoodle.pathing.WINDOWS_EXTENDED_PATH_THRESHOLD", 1)

    assert os.fspath(make_conflict_path(target)).startswith("\\\\?\\")


def test_private_gzip_json_roundtrip_uses_private_permissions(tmp_path):
    target = tmp_path / "session"

    write_private_gzip_json(target, {"format": "test", "value": 1})

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with target.open("rb") as handle:
        assert json.loads(gzip.decompress(handle.read()).decode("utf-8")) == {
            "format": "test",
            "value": 1,
        }
    assert read_private_gzip_json(target, "test data") == {
        "format": "test",
        "value": 1,
    }


def test_private_chmod_warns_on_windows(tmp_path, monkeypatch, caplog):
    target = tmp_path / "session"
    target.write_bytes(b"data")
    monkeypatch.setattr("syncmymoodle.storage.is_windows", lambda: True)

    def missing_pywin32(name):
        raise ImportError(name)

    monkeypatch.setattr("syncmymoodle.storage.importlib.import_module", missing_pywin32)

    chmod_private_best_effort(target, "session cookie")

    assert (
        "Could not restrict permissions for session cookie file on Windows"
        in caplog.text
    )


def test_private_chmod_uses_windows_acl(tmp_path, monkeypatch):
    target = tmp_path / "session"
    target.write_bytes(b"data")
    monkeypatch.setattr("syncmymoodle.storage.is_windows", lambda: True)
    calls = {}

    class FakeACL:
        def __init__(self):
            self.entries = []
            calls["dacl"] = self

        def AddAccessAllowedAce(self, revision, access_mask, sid):
            self.entries.append((revision, access_mask, sid))

    def set_security_info(*args):
        calls["security_info"] = args

    modules = {
        "win32api": SimpleNamespace(
            CloseHandle=lambda handle: calls.setdefault("closed", []).append(handle),
            GetCurrentProcess=lambda: "process",
        ),
        "win32con": SimpleNamespace(TOKEN_QUERY=8),
        "win32security": SimpleNamespace(
            ACL=FakeACL,
            ACL_REVISION=2,
            DACL_SECURITY_INFORMATION=4,
            PROTECTED_DACL_SECURITY_INFORMATION=8,
            SE_FILE_OBJECT=1,
            TokenUser=1,
            GetTokenInformation=lambda token, token_type: ("user-sid",),
            OpenProcessToken=lambda process, access: ("token", process, access),
            SetNamedSecurityInfo=set_security_info,
        ),
        "ntsecuritycon": SimpleNamespace(
            DELETE=4,
            FILE_GENERIC_READ=1,
            FILE_GENERIC_WRITE=2,
        ),
    }

    def fake_import_module(name):
        return modules[name]

    monkeypatch.setattr(
        "syncmymoodle.storage.importlib.import_module", fake_import_module
    )

    chmod_private_best_effort(target, "session cookie")

    assert calls["dacl"].entries == [(2, 7, "user-sid")]
    assert calls["security_info"] == (
        os.path.abspath(target),
        1,
        12,
        None,
        None,
        calls["dacl"],
        None,
    )
    assert calls["closed"] == [("token", "process", 8)]


def test_private_gzip_json_restricts_temp_file_before_writing_on_windows(
    tmp_path, monkeypatch
):
    target = tmp_path / "session"
    restricted_paths = []

    monkeypatch.setattr("syncmymoodle.storage.is_windows", lambda: True)
    monkeypatch.setattr(
        "syncmymoodle.storage.restrict_private_file_windows",
        lambda path: restricted_paths.append(path),
    )

    write_private_gzip_json(target, {"format": "test", "value": 1})

    assert len(restricted_paths) == 2
    assert restricted_paths[0].name.startswith(".session.")
    assert restricted_paths[0].parent == tmp_path
    assert restricted_paths[1] == target
    assert read_private_gzip_json(target, "test data") == {
        "format": "test",
        "value": 1,
    }


def test_private_gzip_json_write_does_not_require_fchmod(tmp_path, monkeypatch):
    target = tmp_path / "session"
    monkeypatch.delattr(os, "fchmod", raising=False)

    write_private_gzip_json(target, {"format": "test", "value": 1})

    assert read_private_gzip_json(target, "test data") == {
        "format": "test",
        "value": 1,
    }
    assert list(tmp_path.glob(".session.*")) == []


def test_private_gzip_json_closes_temp_file_before_cleanup(tmp_path, monkeypatch):
    target = tmp_path / "session"
    real_close = os.close
    closed_fds = []

    def broken_fdopen(fd, mode):
        raise OSError("simulated write setup failure")

    def recording_close(fd):
        closed_fds.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "fdopen", broken_fdopen)
    monkeypatch.setattr(os, "close", recording_close)

    with pytest.raises(OSError, match="simulated write setup failure"):
        write_private_gzip_json(target, {"format": "test", "value": 1})

    assert closed_fds
    assert not target.exists()
    assert list(tmp_path.glob(".session.*")) == []


def test_download_uses_course_cache_to_skip_unchanged_file(tmp_path):
    config = {"basedir": str(tmp_path), "updatefiles": True}
    cached_syncer = make_context(config)
    cached_root = Node("", -1, "Root", None)
    semester = cached_root.add_child("26ss", None, "Semester")
    course = semester.add_child("Cache Behavior", 301, "Course")
    section = course.add_child("General", 401, "Section")
    cached_file = section.add_child(
        "slides.pdf",
        "https://moodle.rwth-aachen.de/pluginfile.php/301/slides.pdf",
        "Linked file [application/pdf]",
        url="https://moodle.rwth-aachen.de/pluginfile.php/301/slides.pdf",
        timemodified=1710000300,
    )
    # A real cache is written after a successful download.
    cached_file.is_downloaded = True
    cached_syncer.root_node = cached_root
    course_cache.cache_root_node(cached_syncer)

    download_path = node_path(cached_syncer, cached_file)
    download_path.parent.mkdir(parents=True, exist_ok=True)
    download_path.write_bytes(b"already downloaded")

    syncer = make_context(config)
    syncer.session = FakeSession()
    current_root = Node("", -1, "Root", None)
    current_semester = current_root.add_child("26ss", None, "Semester")
    current_course = current_semester.add_child("Cache Behavior", 301, "Course")
    current_section = current_course.add_child("General", 401, "Section")
    current_file = current_section.add_child(
        "slides.pdf",
        "https://moodle.rwth-aachen.de/pluginfile.php/301/slides.pdf",
        "Linked file [application/pdf]",
        url="https://moodle.rwth-aachen.de/pluginfile.php/301/slides.pdf",
        timemodified=1710000300,
    )

    assert download_file(syncer, current_file) is True
    assert syncer.session.calls == []
