import json
import logging
import sys
import tomllib
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

import syncmymoodle.cli as cli
from syncmymoodle.config import (
    CONFIG_OPTIONS,
    Config,
    ConfigValidationError,
    validate_config,
)


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
        "chromiumpath": "chromium_path",
        "courseprefix": "course_prefix_handling",
        "nolinks": "nolinks",
        "updatefiles": "updatefiles",
        "updatefilesconflict": "update_files_conflict",
        "courses": "selected_courses",
        "skipcourses": "skip_courses",
        "semester": "only_sync_semester",
        "excludefiletypes": "exclude_filetypes",
        "excludefiles": "exclude_files",
        "excludelinks": "exclude_links",
        "alloweddomains": "allowed_domains",
        "excludesections": "exclude_sections",
        "excludemodules": "exclude_modules",
    }


def test_config_validation_accepts_current_and_legacy_keys():
    validate_config(
        {
            "no_links": True,
            "update_files": True,
            "skip_sections": ["Hidden*"],
            "skip_modules": ["Skip Module"],
            "use_secret_service": False,
            "secret_service_store_totp_secret": False,
            "used_modules": {
                "assign": True,
                "resource": False,
                "folder": True,
                "url": {
                    "youtube": True,
                    "opencast": False,
                    "sciebo": True,
                    "quiz": "html",
                },
            },
        }
    )


def test_config_validation_accepts_grouped_toml_keys():
    validate_config(
        {
            "auth": {"user": "user", "password": "password"},
            "paths": {"basedir": "/tmp/moodle"},
            "courses": {"course_prefix_handling": "suffix"},
            "downloads": {"update_files": True},
            "links": {"no_links": False, "allowed_domains": ["moodle.rwth-aachen.de"]},
            "skip_rules": {"exclude_sections": ["General"]},
            "modules": {"folder": True, "url": {"quiz": "html"}},
        }
    )


def test_config_validation_rejects_unknown_keys():
    with pytest.raises(ConfigValidationError, match="unknown config key"):
        validate_config({"baseddir": "/tmp/moodle"})


def test_config_validation_suggests_similar_keys():
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config({"paths": {"baseidr": "/tmp/moodle"}})

    assert "Did you mean 'paths.basedir'?" in str(exc_info.value)


def test_config_validation_rejects_invalid_choices():
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(
            {
                "course_prefix_handling": "later",
                "update_files_conflict": "delete",
                "used_modules": {"url": {"quiz": "screenshots"}},
            }
        )

    message = str(exc_info.value)
    assert "course_prefix_handling must be one of" in message
    assert "update_files_conflict must be one of" in message
    assert "used_modules.url.quiz must be one of" in message


def test_config_validation_rejects_non_boolean_toggles():
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(
            {
                "no_links": "false",
                "use_secret_service": "yes",
                "used_modules": {
                    "folder": "true",
                    "url": {"youtube": "false"},
                },
            }
        )

    message = str(exc_info.value)
    assert "no_links must be true or false" in message
    assert "use_secret_service must be true or false" in message
    assert "used_modules.folder must be true or false" in message
    assert "used_modules.url.youtube must be true or false" in message


def test_config_validation_rejects_unknown_module_keys():
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(
            {
                "used_modules": {
                    "page": True,
                    "url": {"vimeo": True},
                }
            }
        )

    message = str(exc_info.value)
    assert "used_modules contains unknown key(s): page" in message
    assert "used_modules.url contains unknown key(s): vimeo" in message


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


def test_toml_example_is_valid():
    raw = tomllib.loads(Path("config.toml.example").read_text(encoding="utf-8"))
    validate_config(raw)
    cfg = Config.from_dict(raw)
    assert cfg.basedir == "./"
    assert cfg.course_prefix_handling == "suffix"
    assert cfg.updatefiles is True
    assert cfg.quiz_mode == "html"


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


def test_cli_loads_toml_configs_before_legacy_json(tmp_path, monkeypatch):
    xdg_config_dir = tmp_path / "xdg" / "syncmymoodle"
    xdg_config_dir.mkdir(parents=True)
    (xdg_config_dir / "config.json").write_text(
        json.dumps({"user": "global-json", "password": "json-password"}),
        encoding="utf-8",
    )
    (xdg_config_dir / "config.toml").write_text(
        'user = "global-toml"\npassword = "toml-password"\ntotp = "global-totp"\n',
        encoding="utf-8",
    )
    (tmp_path / "config.json").write_text(
        json.dumps({"user": "local-json", "basedir": "/json"}),
        encoding="utf-8",
    )
    (tmp_path / "config.toml").write_text(
        'user = "local-toml"\nbasedir = "/toml"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)

    parser = cli.build_parser()
    args = parser.parse_args([])

    config = cli.load_config(args, parser)

    assert config == {
        "user": "local-toml",
        "password": "toml-password",
        "totp": "global-totp",
        "basedir": "/toml",
    }


def test_cli_loads_explicit_toml_config(tmp_path):
    config_path = tmp_path / "sync.toml"
    config_path.write_text(
        """
user = "toml-user"
password = "toml-password"
totp = "toml-totp"

[used_modules.url]
quiz = "pdf"
""",
        encoding="utf-8",
    )

    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    assert cli.load_config(args, parser) == {
        "user": "toml-user",
        "password": "toml-password",
        "totp": "toml-totp",
        "used_modules": {"url": {"quiz": "pdf"}},
    }


def test_cli_loads_grouped_toml_config(tmp_path):
    config_path = tmp_path / "sync.toml"
    config_path.write_text(
        """
[auth]
user = "toml-user"
password = "toml-password"
totp = "toml-totp"

[paths]
basedir = "/tmp/moodle"

[courses]
course_prefix_handling = "suffix"

[downloads]
update_files = true

[links]
allowed_domains = ["moodle.rwth-aachen.de"]

[skip_rules]
exclude_sections = ["General"]

[modules.url]
quiz = "pdf"
""",
        encoding="utf-8",
    )

    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    assert cli.load_config(args, parser) == {
        "user": "toml-user",
        "password": "toml-password",
        "totp": "toml-totp",
        "basedir": "/tmp/moodle",
        "course_prefix_handling": "suffix",
        "update_files": True,
        "allowed_domains": ["moodle.rwth-aachen.de"],
        "exclude_sections": ["General"],
        "used_modules": {"url": {"quiz": "pdf"}},
    }


def test_cli_still_loads_explicit_extensionless_json_config(tmp_path, caplog):
    config_path = tmp_path / "syncmymoodle-config"
    config_path.write_text(
        json.dumps({"user": "json-user", "password": "json-password"}),
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="syncmymoodle.cli")

    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    assert cli.load_config(args, parser) == {
        "user": "json-user",
        "password": "json-password",
    }
    assert "legacy JSON config" in caplog.text


def test_cli_warns_when_loading_legacy_json_config(tmp_path, monkeypatch, caplog):
    (tmp_path / "config.json").write_text(
        json.dumps({"user": "json-user"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.WARNING, logger="syncmymoodle.cli")

    parser = cli.build_parser()
    args = parser.parse_args([])

    assert cli.load_config(args, parser) == {"user": "json-user"}
    assert "legacy JSON config" in caplog.text
    assert "syncmymoodle config migrate" in caplog.text


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


def test_config_migrate_command_writes_toml(tmp_path, capsys):
    input_path = tmp_path / "config.json"
    output_path = tmp_path / "config.toml"
    input_path.write_text(
        json.dumps(
            {
                "user": "json-user",
                "password": "json-password",
                "totp": "json-totp",
                "selected_courses": ["course-a", "course-b"],
                "used_modules": {"url": {"quiz": "html"}},
            }
        ),
        encoding="utf-8",
    )

    cli.main(
        [
            "config",
            "migrate",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ]
    )

    migrated = tomllib.loads(output_path.read_text(encoding="utf-8"))
    assert migrated == {
        "auth": {
            "user": "json-user",
            "password": "json-password",
            "totp": "json-totp",
        },
        "courses": {"selected_courses": ["course-a", "course-b"]},
        "modules": {"url": {"quiz": "html"}},
    }
    assert str(output_path) in capsys.readouterr().out


def test_config_check_command_reports_valid_config(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[auth]
user = "user"
password = "password"
totp = "totp"

[courses]
course_prefix_handling = "suffix"
""",
        encoding="utf-8",
    )

    cli.main(["config", "check", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert f"Config is valid: {config_path}" in captured.out
    assert captured.err == ""


def test_config_check_command_reports_validation_errors(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[paths]
baseidr = "/tmp/moodle"

[courses]
course_prefix_handling = "later"
""",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["config", "check", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.out == ""
    assert f"Config is invalid: {config_path}" in captured.err
    assert "- unknown config key 'paths.baseidr'." in captured.err
    assert "Did you mean 'paths.basedir'?" in captured.err
    assert "- course_prefix_handling must be one of" in captured.err


def test_cli_rejects_invalid_config_before_sync(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
user = "user"
password = "password"
totp = "totp"
course_prefix_handling = "later"
""",
        encoding="utf-8",
    )
    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.config_from_args(args, parser)

    assert exc_info.value.code == 2
    assert "course_prefix_handling must be one of" in capsys.readouterr().err


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
            "--chromiumpath",
            "/usr/bin/chromium",
            "--courseprefix",
            "suffix",
            "--secretservice",
            "--secretservicetotpsecret",
            "--nolinks",
            "--excludefiletypes",
            "pdf,mp4",
            "--excludefiles",
            "*.bak,*.tmp",
            "--excludelinks",
            "*calendar*,*hinge*",
            "--alloweddomains",
            "moodle.rwth-aachen.de,rwth-aachen.sciebo.de",
            "--excludesections",
            "General,Week 1",
            "--excludemodules",
            "Quiz*,resource",
            "--quiz",
            "pdf",
            "--updatefiles",
            "--updatefilesconflict",
            "keep",
        ]
    )
    config = {
        "user": "config-user",
        "password": "config-password",
        "totp": "config-totp",
        "used_modules": {
            "folder": False,
            "url": {"opencast": True, "quiz": "off"},
        },
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
        "chromium_path": "/usr/bin/chromium",
        "course_prefix_handling": "suffix",
        "use_secret_service": True,
        "secret_service_store_totp_secret": True,
        "nolinks": True,
        "exclude_filetypes": ["pdf", "mp4"],
        "exclude_files": ["*.bak", "*.tmp"],
        "exclude_links": ["*calendar*", "*hinge*"],
        "allowed_domains": ["moodle.rwth-aachen.de", "rwth-aachen.sciebo.de"],
        "exclude_sections": ["General", "Week 1"],
        "exclude_modules": ["Quiz*", "resource"],
        "used_modules": {
            "folder": False,
            "url": {"opencast": True, "quiz": "pdf"},
        },
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
