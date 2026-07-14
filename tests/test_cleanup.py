from pathlib import Path

import pytest

import syncmymoodle.cli as cli
from syncmymoodle import cleanup, pathing
from syncmymoodle.constants import COURSE_CACHE_FILENAME


def write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_conflict_cleanup_plan_removes_only_redundant_conflicts(tmp_path):
    current = write(tmp_path / "course" / "file.pdf", b"current")
    same_as_current = write(
        current.with_name("file.syncconflict.aaaaaaaa.pdf"), b"current"
    )
    unique = write(current.with_name("file.syncconflict.bbbbbbbb.pdf"), b"unique")
    duplicate_to_keep = write(
        current.with_name("file.syncconflict.cccccccc.pdf"), b"duplicate"
    )
    duplicate_to_remove = write(
        current.with_name("file.syncconflict.dddddddd.2.pdf"), b"duplicate"
    )

    conflicts = cleanup.iter_conflicts(tmp_path)
    plan = cleanup.conflict_cleanup_plan(conflicts)

    assert plan.remove == tuple(sorted([same_as_current, duplicate_to_remove]))
    assert {conflict.path for conflict in plan.keep} == {unique, duplicate_to_keep}


def test_cleanup_ignores_unique_conflicts_without_current_file(tmp_path):
    conflict = write(
        tmp_path / "course" / "missing.syncconflict.aaaaaaaa.pdf", b"user changes"
    )

    plan = cleanup.conflict_cleanup_plan(cleanup.iter_conflicts(tmp_path))

    assert plan.remove == ()
    assert [kept.path for kept in plan.keep] == [conflict]


def test_cleanup_recognizes_generated_conflict_paths(tmp_path):
    current = write(tmp_path / "course" / "file.pdf", b"content")
    conflict = pathing.make_conflict_path(current)
    write(conflict, b"conflict")
    indexed_conflict = pathing.make_conflict_path(current)

    conflict_path = pathing.parse_conflict_path(indexed_conflict)

    assert conflict_path is not None
    assert conflict_path.canonical == current
    assert conflict_path.index == 1
    assert cleanup.iter_conflicts(tmp_path)[0].canonical == current


def test_cleanup_does_not_confuse_numeric_extension_with_copy_index(tmp_path):
    current = write(tmp_path / "lecture.1", b"new remote content")
    conflict = pathing.make_conflict_path(current)
    write(conflict, b"unique local edit")
    write(tmp_path / "lecture", b"unique local edit")

    parsed = pathing.parse_conflict_path(conflict)
    plan = cleanup.conflict_cleanup_plan(cleanup.iter_conflicts(tmp_path))

    assert parsed is not None
    assert parsed.canonical == current
    assert plan.remove == ()
    assert [kept.path for kept in plan.keep] == [conflict]


def test_cleanup_ignores_ambiguous_legacy_numeric_conflict(tmp_path):
    legacy_conflict = write(
        tmp_path / "lecture.syncconflict.aaaaaaaa.1", b"unique local edit"
    )
    write(tmp_path / "lecture", b"unique local edit")

    assert pathing.parse_conflict_path(legacy_conflict) is None
    assert cleanup.iter_conflicts(tmp_path) == []


def test_iter_course_caches_finds_only_cache_files(tmp_path):
    cache = write(tmp_path / "course" / COURSE_CACHE_FILENAME, b"{}")
    write(tmp_path / "course" / "notes.syncmymoodle_cache", b"not a cache")

    assert cleanup.iter_course_caches(tmp_path) == [cache]


@pytest.mark.parametrize("command", ["conflicts", "caches"])
def test_clean_help_makes_dry_run_default_explicit(command, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["clean", command, "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "default is a dry run" in output
    assert "--apply" in output


def test_clean_conflicts_dry_run_uses_config_without_credentials(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "Moodle"
    current = write(root / "course" / "file.pdf", b"content")
    conflict = write(current.with_name("file.syncconflict.aaaaaaaa.pdf"), b"content")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[paths]\nsync_directory = "Moodle"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("NO_COLOR", raising=False)

    cli.main(
        [
            "--config",
            str(config_path),
            "--color",
            "always",
            "clean",
            "conflicts",
        ]
    )

    captured = capsys.readouterr()
    assert f"Would delete: {conflict}" in captured.out
    assert f"\x1b[33mWould delete: {conflict}\x1b[0m" in captured.out
    assert "Dry run only." in captured.out
    assert captured.err == ""
    assert conflict.exists()


def test_clean_conflicts_apply_deletes_redundant_conflicts(
    tmp_path, monkeypatch, capsys
):
    current = write(tmp_path / "course" / "file.pdf", b"content")
    conflict = write(current.with_name("file.syncconflict.aaaaaaaa.pdf"), b"content")
    monkeypatch.delenv("NO_COLOR", raising=False)

    cli.main(
        [
            "--color",
            "always",
            "clean",
            "conflicts",
            "--path",
            str(tmp_path),
            "--apply",
        ]
    )

    assert not conflict.exists()
    output = capsys.readouterr().out
    assert f"\x1b[32mDeleted: {conflict}\x1b[0m" in output
    assert "1 file deleted" in output


def test_clean_apply_requires_explicit_or_configured_path(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["clean", "conflicts", "--apply"])

    assert exc_info.value.code == 2
    assert (
        "requires --path or a configured paths.sync_directory"
        in capsys.readouterr().err
    )


def test_clean_caches_apply_deletes_course_caches(tmp_path, capsys):
    cache = write(tmp_path / "course" / COURSE_CACHE_FILENAME, b"{}")

    cli.main(["clean", "caches", "--path", str(tmp_path), "--apply"])

    assert not cache.exists()
    output = capsys.readouterr().out
    assert "metadata caches" in output
    assert "1 cache file deleted" in output


def test_clean_rejects_missing_path(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["clean", "conflicts", "--path", str(tmp_path / "missing")])

    assert exc_info.value.code == 2


def test_clean_rejects_sync_options_without_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--max-file-size", "huge", "clean", "conflicts"])

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "sync options cannot be used with `clean`: --max-file-size" in captured.err
    assert "Traceback" not in captured.err
