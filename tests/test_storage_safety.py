import gzip
import hashlib
import importlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from syncmymoodle import pathing
from syncmymoodle.node import Node
from syncmymoodle.pathing import (
    PATH_COMPONENT_MAX_BYTES,
    format_conflict_path,
    make_conflict_path,
    sanitize_path_part,
    windows_extended_length_path,
)
from syncmymoodle.storage import (
    InstallResult,
    SyncRunLockedError,
    chmod_private_best_effort,
    install_staged_file,
    load_session_from_data,
    read_private_gzip_json,
    save_session,
    session_to_data,
    snapshot_file,
    sync_run_lock,
    write_private_gzip_json,
    write_private_text,
)

from .helpers import make_context, node_path


def test_sanitized_node_path_stays_inside_basedir(tmp_path):
    syncer = make_context({"paths.sync_directory": str(tmp_path)})
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


def test_sanitize_path_part_decodes_entities_idempotently():
    sanitized = sanitize_path_part("R&amp;amp;D")

    assert sanitized == "R&D"
    assert sanitize_path_part(sanitized) == sanitized


def test_sanitize_path_part_bounds_long_names_and_preserves_extension():
    first = sanitize_path_part(f"lecture-{'a' * 300}-one.pdf")
    second = sanitize_path_part(f"lecture-{'a' * 300}-two.pdf")

    assert len(first.encode("utf-8")) <= PATH_COMPONENT_MAX_BYTES
    assert first.startswith("lecture-")
    assert first.endswith(".pdf")
    assert first != second
    assert sanitize_path_part(first) == first


def test_sanitize_path_part_applies_limit_to_multibyte_names():
    sanitized = sanitize_path_part(f"{'資料' * 150}.pdf")

    assert len(sanitized.encode("utf-8")) <= PATH_COMPONENT_MAX_BYTES
    assert sanitized.endswith(".pdf")


def test_bounded_name_leaves_room_for_internal_suffixes():
    sanitized = sanitize_path_part(f"{'a' * 300}.pdf")
    conflict_name = format_conflict_path(Path(sanitized), "12345678", 999).name

    assert len(f".{sanitized}.smmpart.etag".encode("utf-8")) <= 255
    assert len(conflict_name.encode("utf-8")) <= 255


def test_empty_child_node_name_materializes_as_placeholder(tmp_path):
    syncer = make_context({"paths.sync_directory": str(tmp_path)})
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


def test_user_config_dir_uses_xdg_override(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(pathing, "is_windows", lambda: True)

    assert pathing.user_config_dir() == tmp_path / "xdg" / "syncmymoodle"


def test_user_config_dir_returns_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", "xdg")

    assert pathing.user_config_dir() == tmp_path / "xdg" / "syncmymoodle"


def test_user_config_dir_uses_windows_appdata(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(pathing, "is_windows", lambda: True)

    assert pathing.user_config_dir() == tmp_path / "appdata" / "syncmymoodle"


@pytest.mark.parametrize(
    ("platform", "windows", "parts"),
    [
        ("linux", False, (".config",)),
        ("darwin", False, ("Library", "Application Support")),
        ("win32", True, ("AppData", "Roaming")),
    ],
)
def test_user_config_dir_uses_platform_default_without_environment_override(
    tmp_path,
    monkeypatch,
    platform,
    windows,
    parts,
):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(pathing.sys, "platform", platform)
    monkeypatch.setattr(pathing, "is_windows", lambda: windows)

    assert pathing.user_config_dir() == tmp_path.joinpath(*parts, "syncmymoodle")


def test_conflict_path_applies_windows_prefix_after_suffix(tmp_path, monkeypatch):
    target = tmp_path / "file.pdf"
    target.write_bytes(b"content")
    monkeypatch.setattr("syncmymoodle.pathing.is_windows", lambda: True)
    monkeypatch.setattr("syncmymoodle.pathing.WINDOWS_EXTENDED_PATH_THRESHOLD", 1)

    assert os.fspath(make_conflict_path(target)).startswith("\\\\?\\")


def test_private_gzip_json_roundtrip_uses_private_permissions(tmp_path):
    target = tmp_path / "session"

    write_private_gzip_json(target, {"format": "test", "value": 1})

    if os.name != "nt":
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


def test_session_cache_roundtrip_includes_session_key(tmp_path):
    target = tmp_path / "session"
    cookies = requests.cookies.RequestsCookieJar()
    cookies.set("MoodleSession", "cookie-value", domain="moodle.example", path="/")

    save_session(target, cookies, "sesskey-value")

    payload = read_private_gzip_json(target, "session cookie")
    restored = requests.cookies.RequestsCookieJar()
    assert payload["format"] == "syncmymoodle.session.v2"
    assert load_session_from_data(restored, payload) == "sesskey-value"
    assert restored.get("MoodleSession", domain="moodle.example", path="/") == (
        "cookie-value"
    )


def test_legacy_cookie_cache_loads_without_session_key():
    cookies = requests.cookies.RequestsCookieJar()
    payload = session_to_data(cookies, "unused")
    payload["format"] = "syncmymoodle.cookies.v1"
    payload.pop("session_key")

    assert load_session_from_data(cookies, payload) is None


@pytest.mark.parametrize(
    "cookie_data_items",
    [
        1,
        [{"name": "valid", "value": "restorable"}, "not an object"],
    ],
)
def test_malformed_cookie_list_is_ignored_without_partial_restore(
    caplog, cookie_data_items
):
    cookies = requests.cookies.RequestsCookieJar()
    payload = {
        "format": "syncmymoodle.session.v2",
        "session_key": "session-key",
        "cookies": cookie_data_items,
    }

    assert load_session_from_data(cookies, payload) is None
    assert list(cookies) == []
    assert "Ignoring malformed cookie file" in caplog.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("value", 123),
        ("domain", 123),
        ("path", 123),
        ("secure", {}),
        ("expires", {}),
        ("rest", {"HttpOnly": 123}),
    ],
)
def test_malformed_cookie_fields_are_ignored_without_restore(caplog, field, value):
    cookies = requests.cookies.RequestsCookieJar()
    cookie = {
        "name": "MoodleSession",
        "value": "cookie-value",
        "domain": "moodle.example",
        "path": "/",
        "secure": True,
        "expires": None,
        "rest": {"HttpOnly": None},
    }
    cookie[field] = value
    payload = {
        "format": "syncmymoodle.session.v2",
        "session_key": "session-key",
        "cookies": [cookie],
    }

    assert load_session_from_data(cookies, payload) is None
    assert list(cookies) == []
    assert "Ignoring malformed cookie file" in caplog.text


def test_private_text_write_is_atomic_and_private(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    write_private_text(target, "original", "config")
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def failed_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", failed_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        write_private_text(target, "replacement", "config")

    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".config.toml.*")) == []


def test_failed_conflict_install_restores_original_file(tmp_path, monkeypatch):
    target = tmp_path / "slides.pdf"
    staged = tmp_path / ".slides.pdf.smmpart"
    target.write_bytes(b"local edit")
    staged.write_bytes(b"remote update")
    baseline = snapshot_file(target)

    monkeypatch.setattr(
        os,
        "replace",
        lambda source, destination: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = install_staged_file(
        staged,
        target,
        baseline=baseline,
        rename_local=True,
        target_change_policy="rename",
        description="test update",
    )

    assert result is InstallResult.FAILED
    assert target.read_bytes() == b"local edit"
    assert list(tmp_path.glob("*.syncconflict.*")) == []


def test_file_snapshot_captures_common_digests_in_one_baseline(tmp_path):
    target = tmp_path / "slides.pdf"
    content = b"snapshot contents"
    target.write_bytes(content)

    baseline = snapshot_file(target)

    assert (
        baseline.digest_for("md5")
        == hashlib.md5(content, usedforsecurity=False).hexdigest()
    )
    assert (
        baseline.digest_for("sha1")
        == hashlib.sha1(content, usedforsecurity=False).hexdigest()
    )
    assert baseline.digest_for("sha256") == hashlib.sha256(content).hexdigest()
    assert baseline.metadata_still_matches(target)
    assert baseline.still_matches(target)


def test_sync_run_lock_rejects_a_concurrent_writer(tmp_path):
    with sync_run_lock(tmp_path):
        with pytest.raises(SyncRunLockedError, match="another sync is already using"):
            with sync_run_lock(tmp_path):
                pass

    with sync_run_lock(tmp_path):
        pass


def test_private_text_restricts_temp_before_writing_on_windows(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    restricted_paths = []
    monkeypatch.setattr("syncmymoodle.pathing.is_windows", lambda: True)
    monkeypatch.setattr(
        "syncmymoodle.storage.restrict_private_file_windows",
        lambda path: restricted_paths.append(path),
    )

    write_private_text(target, "[auth]\n", "config")

    assert len(restricted_paths) == 1
    assert restricted_paths[0].name.startswith(".config.toml.")
    assert target.read_text(encoding="utf-8") == "[auth]\n"


def test_private_text_aborts_when_temp_permissions_cannot_be_restricted(
    tmp_path, monkeypatch
):
    target = tmp_path / "config.toml"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr("syncmymoodle.pathing.is_windows", lambda: True)
    monkeypatch.setattr(
        "syncmymoodle.storage.restrict_private_file_windows",
        lambda path: (_ for _ in ()).throw(OSError("ACL denied")),
    )

    with pytest.raises(PermissionError, match="temporary config"):
        write_private_text(target, "secret", "config")

    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".config.toml.*")) == []


def test_private_text_aborts_when_fchmod_fails(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setattr("syncmymoodle.pathing.is_windows", lambda: False)
    monkeypatch.setattr(
        os,
        "fchmod",
        lambda fd, mode: (_ for _ in ()).throw(OSError("chmod denied")),
        raising=False,
    )

    with pytest.raises(PermissionError, match="temporary config"):
        write_private_text(target, "secret", "config")

    assert not target.exists()
    assert list(tmp_path.glob(".config.toml.*")) == []


def test_private_chmod_warns_on_windows(tmp_path, monkeypatch, caplog):
    target = tmp_path / "session"
    target.write_bytes(b"data")
    monkeypatch.setattr("syncmymoodle.pathing.is_windows", lambda: True)

    def missing_pywin32(name):
        raise ImportError(name)

    monkeypatch.setattr("syncmymoodle.storage.importlib.import_module", missing_pywin32)

    result = chmod_private_best_effort(target, "session cookie")

    assert result is False
    assert (
        "Could not restrict permissions for session cookie file on Windows"
        in caplog.text
    )


def test_private_chmod_uses_windows_acl(tmp_path, monkeypatch):
    target = tmp_path / "session"
    target.write_bytes(b"data")
    monkeypatch.setattr("syncmymoodle.pathing.is_windows", lambda: True)
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

    assert chmod_private_best_effort(target, "session cookie") is True

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


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ACL APIs")
def test_private_text_write_persists_windows_acl(tmp_path):
    win32api = importlib.import_module("win32api")
    win32con = importlib.import_module("win32con")
    win32security = importlib.import_module("win32security")
    ntsecuritycon = importlib.import_module("ntsecuritycon")
    target = tmp_path / "config.toml"
    target.write_text("original", encoding="utf-8")

    write_private_text(target, "secret", "config")

    assert target.read_text(encoding="utf-8") == "secret"
    descriptor = win32security.GetNamedSecurityInfo(
        os.fspath(target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    control, _revision = descriptor.GetSecurityDescriptorControl()
    assert control & win32security.SE_DACL_PROTECTED

    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None
    assert dacl.IsValid()
    assert dacl.GetAceCount() == 1
    (ace_type, ace_flags), access_mask, ace_sid = dacl.GetAce(0)
    assert ace_type == win32security.ACCESS_ALLOWED_ACE_TYPE
    assert ace_flags == 0
    assert access_mask == (
        ntsecuritycon.FILE_GENERIC_READ
        | ntsecuritycon.FILE_GENERIC_WRITE
        | ntsecuritycon.DELETE
    )

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        win32api.CloseHandle(token)
    assert win32security.ConvertSidToStringSid(ace_sid) == (
        win32security.ConvertSidToStringSid(user_sid)
    )


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
