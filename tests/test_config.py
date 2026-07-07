import json
import sys
from dataclasses import fields
from types import SimpleNamespace

import syncmymoodle.cli as cli
from syncmymoodle.config import CONFIG_OPTIONS, Config


def test_config_options_cover_typed_config_fields():
    assert tuple(field.name for field in fields(Config)) == tuple(
        option.field_name for option in CONFIG_OPTIONS
    )


def test_config_options_record_existing_cli_overrides():
    assert {
        option.cli.arg_name: option.canonical_key
        for option in CONFIG_OPTIONS
        if option.cli
    } == {
        "user": "user",
        "password": "password",
        "totp": "totp",
        "totpsecret": "totpsecret",
        "cookiefile": "cookie_file",
        "basedir": "basedir",
        "courseprefix": "course_prefix_handling",
        "nolinks": "nolinks",
        "updatefiles": "updatefiles",
        "updatefilesconflict": "update_files_conflict",
        "courses": "selected_courses",
        "skipcourses": "skip_courses",
        "semester": "only_sync_semester",
        "excludefiletypes": "exclude_filetypes",
    }


def test_defaults_applied_for_empty_config():
    cfg = Config.from_dict({})
    assert cfg.basedir == "./"
    assert cfg.cookie_file == "./session"
    assert cfg.course_prefix_handling == "keep"
    assert cfg.update_files_conflict == "rename"
    assert cfg.nolinks is False
    assert cfg.updatefiles is False
    assert cfg.selected_courses == []
    assert cfg.exclude_links == {}
    # A default module tree is provided when none is configured.
    assert cfg.module_enabled("assign")
    assert cfg.module_enabled("folder")
    assert cfg.url_module_enabled("opencast")


def test_legacy_key_aliases_are_resolved():
    cfg = Config.from_dict(
        {
            "no_links": True,
            "update_files": True,
            "skip_sections": ["Hidden*"],
            "skip_modules": ["Skip Module"],
        }
    )
    assert cfg.nolinks is True
    assert cfg.updatefiles is True
    assert cfg.exclude_sections == {"*": ["Hidden*"]}
    assert cfg.exclude_modules == {"*": ["Skip Module"]}


def test_canonical_keys_win_over_aliases():
    cfg = Config.from_dict(
        {
            "nolinks": False,
            "no_links": True,
            "updatefiles": False,
            "update_files": True,
        }
    )
    assert cfg.nolinks is False
    assert cfg.updatefiles is False


def test_quiz_mode_normalizes_values():
    # Legacy booleans map onto the mode strings.
    assert (
        Config.from_dict({"used_modules": {"url": {"quiz": True}}}).quiz_mode == "both"
    )
    assert (
        Config.from_dict({"used_modules": {"url": {"quiz": False}}}).quiz_mode == "off"
    )
    # Explicit modes are passed through (case-insensitively).
    for mode in ("off", "html", "pdf", "both"):
        cfg = Config.from_dict({"used_modules": {"url": {"quiz": mode.upper()}}})
        assert cfg.quiz_mode == mode
    # Unrecognized values disable quizzes rather than crashing.
    assert (
        Config.from_dict({"used_modules": {"url": {"quiz": "wat"}}}).quiz_mode == "off"
    )


def test_quiz_enabled_helper_tracks_mode():
    on = Config.from_dict({"used_modules": {"url": {"quiz": "pdf", "opencast": True}}})
    assert on.url_module_enabled("quiz") is True
    assert on.url_module_enabled("opencast") is True
    off = Config.from_dict({"used_modules": {"url": {"quiz": "off"}}})
    assert off.url_module_enabled("quiz") is False


def test_quiz_defaults_to_html_when_no_modules_configured():
    # HTML is the safe default: it archives e-tests without launching a browser.
    assert Config.from_dict({}).quiz_mode == "html"


def test_from_dict_does_not_mutate_input():
    raw = {"used_modules": {"url": {"quiz": True, "opencast": True}}}
    cfg = Config.from_dict(raw)
    assert cfg.quiz_mode == "both"
    assert raw["used_modules"]["url"]["quiz"] is True


def test_module_helpers_reflect_toggles():
    cfg = Config.from_dict(
        {
            "used_modules": {
                "assign": False,
                "folder": True,
                "url": {"youtube": False, "sciebo": True},
            }
        }
    )
    assert cfg.module_enabled("assign") is False
    assert cfg.module_enabled("folder") is True
    assert cfg.module_enabled("url") is True  # non-empty url dict is truthy
    assert cfg.url_module_enabled("youtube") is False
    assert cfg.url_module_enabled("sciebo") is True
    assert cfg.url_module_enabled("missing") is False


def test_from_dict_accepts_none():
    cfg = Config.from_dict(None)
    assert cfg.basedir == "./"


def test_filter_values_are_normalized():
    cfg = Config.from_dict(
        {
            "selected_courses": 12,
            "exclude_links": "*calendar*",
            "allowed_domains": "moodle.rwth-aachen.de",
            "exclude_sections": {"*": "General", 42: ["Hidden", None]},
            "exclude_modules": {"42": "Quiz*"},
        }
    )

    assert cfg.selected_courses == ["12"]
    assert cfg.exclude_links == {"*": ["*calendar*"]}
    assert cfg.allowed_domains == {"*": ["moodle.rwth-aachen.de"]}
    assert cfg.exclude_sections == {"*": ["General"], "42": ["Hidden"]}
    assert cfg.exclude_modules == {"42": ["Quiz*"]}


def test_cli_loads_global_then_local_config(tmp_path, monkeypatch):
    xdg_config = tmp_path / "xdg" / "syncmymoodle" / "config.json"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text(
        json.dumps(
            {
                "user": "global-user",
                "password": "global-password",
                "totp": "global-totp",
                "basedir": "/global",
            }
        ),
        encoding="utf-8",
    )
    local_config = tmp_path / "config.json"
    local_config.write_text(
        json.dumps({"user": "local-user", "basedir": "/local"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)

    parser = cli.build_parser()
    args = parser.parse_args([])

    config = cli.load_config(args, parser)

    assert config["user"] == "local-user"
    assert config["password"] == "global-password"
    assert config["totp"] == "global-totp"
    assert config["basedir"] == "/local"


def test_cli_explicit_config_skips_discovery(tmp_path, monkeypatch):
    xdg_config = tmp_path / "xdg" / "syncmymoodle" / "config.json"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text(json.dumps({"user": "global-user"}), encoding="utf-8")
    (tmp_path / "config.json").write_text(
        json.dumps({"user": "local-user"}), encoding="utf-8"
    )
    explicit_config = tmp_path / "chosen.json"
    explicit_config.write_text(
        json.dumps(
            {
                "user": "explicit-user",
                "password": "explicit-password",
                "totp": "explicit-totp",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)

    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(explicit_config)])

    assert cli.load_config(args, parser) == {
        "user": "explicit-user",
        "password": "explicit-password",
        "totp": "explicit-totp",
    }


def test_cli_overrides_are_applied_after_config():
    fake_keyring = object()
    parser = cli.build_parser(fake_keyring)
    args = parser.parse_args(
        [
            "--user",
            "cli-user",
            "--password",
            "cli-password",
            "--totp",
            "cli-totp",
            "--totpsecret",
            "cli-totp-secret",
            "--cookiefile",
            "/tmp/session",
            "--courses",
            "course-a,course-b",
            "--skipcourses",
            "course-c,course-d",
            "--semester",
            "25ws,26ss",
            "--basedir",
            "/tmp/moodle",
            "--courseprefix",
            "suffix",
            "--secretservice",
            "--secretservicetotpsecret",
            "--nolinks",
            "--excludefiletypes",
            "pdf,mp4",
            "--updatefiles",
            "--updatefilesconflict",
            "keep",
        ]
    )
    config = {
        "user": "config-user",
        "password": "config-password",
        "totp": "config-totp",
    }

    cli.apply_cli_overrides(config, args, fake_keyring)

    assert config == {
        "user": "cli-user",
        "password": "cli-password",
        "totp": "cli-totp",
        "totpsecret": "cli-totp-secret",
        "cookie_file": "/tmp/session",
        "selected_courses": ["course-a", "course-b"],
        "skip_courses": ["course-c", "course-d"],
        "only_sync_semester": ["25ws", "26ss"],
        "basedir": "/tmp/moodle",
        "course_prefix_handling": "suffix",
        "use_secret_service": True,
        "secret_service_store_totp_secret": True,
        "nolinks": True,
        "exclude_filetypes": ["pdf", "mp4"],
        "updatefiles": True,
        "update_files_conflict": "keep",
    }


def test_cli_keyring_resolution_reads_password_and_totp_secret():
    calls = []
    fake_keyring = SimpleNamespace(
        get_password=lambda service, name: (
            calls.append((service, name))
            or {
                "user": "stored-password",
                "totp-provider": "stored-totp-secret",
            }[name]
        ),
        set_password=lambda service, name, value: calls.append((service, name, value)),
    )
    parser = cli.build_parser(fake_keyring)
    args = parser.parse_args(
        [
            "--user",
            "user",
            "--totp",
            "totp-provider",
            "--secretservice",
            "--secretservicetotpsecret",
        ]
    )
    config = {}

    cli.apply_cli_overrides(config, args, fake_keyring)
    cli.resolve_keyring_credentials(config, args, fake_keyring)

    assert config["password"] == "stored-password"
    assert config["totpsecret"] == "stored-totp-secret"
    assert calls == [
        ("syncmymoodle", "user"),
        ("syncmymoodle", "totp-provider"),
    ]


def test_cli_preserves_canonical_config_keys(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "user": "user",
                "password": "password",
                "totp": "totp",
                "nolinks": True,
                "updatefiles": True,
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def fake_run(ctx):
        captured["config"] = ctx.config

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["syncmymoodle", "--config", str(config_path)],
    )

    cli.main()

    assert captured["config"].nolinks is True
    assert captured["config"].updatefiles is True
