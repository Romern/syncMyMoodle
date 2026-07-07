import json
import logging
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import syncmymoodle.cli as cli
from syncmymoodle.config import (
    CONFIG_OPTIONS,
    LEGACY_KEY_MAP,
    Config,
    ConfigValidationError,
    canonicalize,
    convert_legacy_config,
    validate_config,
)


def test_config_options_record_cli_overrides():
    assert {
        option.cli.arg_name: option.canonical_key
        for option in CONFIG_OPTIONS
        if option.cli
    } == {
        "user": "auth.user",
        "password": "auth.password",
        "totp-serial": "auth.totp_serial",
        "totp-secret": "auth.totp_secret",
        "use-keyring": "auth.use_keyring",
        "keyring-store-totp-secret": "auth.keyring_store_totp_secret",
        "sync-directory": "paths.sync_directory",
        "cookie-file": "paths.cookie_file",
        "browser": "paths.browser",
        "courses": "courses.selected",
        "skip-courses": "courses.skip",
        "semesters": "courses.semesters",
        "course-prefix-handling": "courses.prefix_handling",
        "update-files": "downloads.update_files",
        "conflict-handling": "downloads.conflict_handling",
        "dry-run": "downloads.dry_run",
        "exclude-filetypes": "filters.exclude_filetypes",
        "max-file-size": "filters.max_file_size",
        "min-file-size": "filters.min_file_size",
        "exclude-files": "filters.exclude_files",
        "exclude-links": "filters.exclude_links",
        "allowed-domains": "filters.allowed_domains",
        "exclude-sections": "filters.exclude_sections",
        "exclude-modules": "filters.exclude_modules",
        "no-follow-links": "links.follow_links",
        "quiz": "modules.quiz",
    }


def test_deprecated_cli_flag_spellings_still_work():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--skipcourses", "a,b", "--nolinks", "--updatefilesconflict", "keep"]
    )
    config: dict = {}

    cli.apply_cli_overrides(config, args)

    assert config == {
        "courses.skip": ["a", "b"],
        "links.follow_links": False,
        "downloads.conflict_handling": "keep",
    }
    # The deprecated spellings are accepted but hidden from --help.
    help_text = parser.format_help()
    assert "--skip-courses" in help_text
    assert "--skipcourses" not in help_text


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])

    assert exc_info.value.code == 0
    assert "syncmymoodle" in capsys.readouterr().out


def test_max_file_size_parses_sizes():
    assert (
        Config.from_dict({"filters": {"max_file_size": "500M"}}).max_file_size
        == 500 * 1024**2
    )
    assert Config.from_dict(
        {"filters": {"max_file_size": "1.5g"}}
    ).max_file_size == int(1.5 * 1024**3)
    assert Config.from_dict({"filters": {"max_file_size": 2048}}).max_file_size == 2048
    # Falsey values mean "no limit".
    assert Config.from_dict({}).max_file_size is None
    assert Config.from_dict({"filters": {"max_file_size": 0}}).max_file_size is None
    assert (
        Config.from_dict({"filters": {"min_file_size": "10K"}}).min_file_size
        == 10 * 1024
    )
    with pytest.raises(
        ConfigValidationError, match="filters.max_file_size must be a size"
    ):
        validate_config({"filters": {"max_file_size": "huge"}})
    with pytest.raises(
        ConfigValidationError, match="filters.min_file_size must be a size"
    ):
        validate_config({"filters": {"min_file_size": "tiny"}})
    with pytest.raises(
        ConfigValidationError, match="filters.max_file_size must be a size"
    ):
        validate_config({"filters": {"max_file_size": "1iB"}})
    with pytest.raises(
        ConfigValidationError, match="filters.max_file_size must be a size"
    ):
        validate_config({"filters": {"max_file_size": 0.5}})
    with pytest.raises(
        ConfigValidationError, match="filters.max_file_size must be a size"
    ):
        validate_config({"filters": {"max_file_size": "0.5"}})


def test_converted_legacy_config_passes_validation():
    validate_config(
        convert_legacy_config(
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
    )


def test_config_validation_accepts_grouped_toml_keys():
    validate_config(
        {
            "auth": {"user": "user", "password": "password"},
            "paths": {"sync_directory": "/tmp/moodle"},
            "courses": {"prefix_handling": "suffix", "selected": ["a"]},
            "downloads": {"update_files": True},
            "filters": {
                "allowed_domains": ["moodle.rwth-aachen.de"],
                "exclude_sections": ["General"],
            },
            "links": {"follow_links": True, "youtube": False},
            "modules": {"folder": True, "quiz": "html"},
        }
    )


def test_config_validation_rejects_unknown_keys():
    with pytest.raises(ConfigValidationError, match="unknown config key"):
        validate_config({"baseddir": "/tmp/moodle"})


def test_config_validation_suggests_similar_keys():
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config({"paths": {"sync_directoy": "/tmp/moodle"}})

    assert "Did you mean 'paths.sync_directory'?" in str(exc_info.value)


def test_config_validation_rejects_invalid_choices():
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(
            {
                "courses": {"prefix_handling": "later"},
                "downloads": {"conflict_handling": "delete"},
                "modules": {"quiz": "screenshots"},
            }
        )

    message = str(exc_info.value)
    assert "courses.prefix_handling must be one of" in message
    assert "downloads.conflict_handling must be one of" in message
    assert "modules.quiz must be one of" in message


def test_config_validation_rejects_non_boolean_toggles():
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(
            {
                "auth": {"use_keyring": "yes"},
                "links": {"follow_links": "false", "youtube": "false"},
                "modules": {"folder": "true"},
            }
        )

    message = str(exc_info.value)
    assert "links.follow_links must be true or false" in message
    assert "auth.use_keyring must be true or false" in message
    assert "modules.folder must be true or false" in message
    assert "links.youtube must be true or false" in message


def test_config_from_dict_rejects_invalid_values():
    with pytest.raises(ConfigValidationError, match="links.follow_links"):
        Config.from_dict({"links": {"follow_links": "false"}})
    with pytest.raises(ConfigValidationError, match="modules.quiz"):
        Config.from_dict({"modules": {"quiz": "HTML"}})
    with pytest.raises(ConfigValidationError, match="filters.max_file_size"):
        Config.from_dict({"filters": {"max_file_size": "huge"}})


def test_config_validation_rejects_unknown_legacy_module_keys():
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(
            convert_legacy_config(
                {
                    "used_modules": {
                        "page": True,
                        "url": {"vimeo": True},
                    }
                }
            )
        )

    message = str(exc_info.value)
    assert "unknown config key 'used_modules.page'" in message
    assert "unknown config key 'used_modules.url.vimeo'" in message


def test_config_validation_rejects_plain_values_for_group_tables():
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config({"auth": "user", "modules": "on"})

    message = str(exc_info.value)
    assert "auth must be a table of settings" in message
    assert "modules must be a table of settings" in message


def test_config_validation_hints_at_legacy_keys():
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config({"nolinks": True, "updatefiles": True, "used_modules": {}})

    message = str(exc_info.value)
    assert (
        "'nolinks' is a legacy config key; "
        "use 'links.follow_links' (with the inverted value) instead" in message
    )
    assert (
        "'updatefiles' is a legacy config key; "
        "use 'downloads.update_files' instead" in message
    )
    assert (
        "'used_modules' is a legacy config key; "
        "use the [modules] and [links] tables instead" in message
    )


def test_legacy_key_map_targets_canonical_keys():
    canonical_keys = {option.canonical_key for option in CONFIG_OPTIONS}
    assert set(LEGACY_KEY_MAP.values()) <= canonical_keys


def test_defaults_applied_for_empty_config():
    cfg = Config.from_dict({})
    assert cfg.sync_directory == "./"
    assert cfg.cookie_file == "./session"
    assert cfg.course_prefix_handling == "keep"
    assert cfg.conflict_handling == "rename"
    assert cfg.follow_links is True
    assert cfg.update_files is False
    assert cfg.selected_courses == []
    assert cfg.exclude_links == {}
    # Default module and link toggles are on.
    assert cfg.module_enabled("assignment")
    assert cfg.module_enabled("folder")
    assert cfg.link_source_enabled("opencast")
    assert cfg.quiz_mode == "html"


def test_legacy_key_aliases_are_resolved():
    cfg = Config.from_dict(
        convert_legacy_config(
            {
                "no_links": True,
                "update_files": True,
                "skip_sections": ["Hidden*"],
                "skip_modules": ["Skip Module"],
                "only_sync_semester": ["22s"],
            }
        )
    )
    assert cfg.follow_links is False
    assert cfg.update_files is True
    assert cfg.exclude_sections == {"*": ["Hidden*"]}
    assert cfg.exclude_modules == {"*": ["Skip Module"]}
    assert cfg.only_sync_semester == ["22s"]


def test_legacy_nolinks_values_invert_into_follow_links():
    assert (
        Config.from_dict(convert_legacy_config({"nolinks": True})).follow_links is False
    )
    assert (
        Config.from_dict(convert_legacy_config({"no_links": False})).follow_links
        is True
    )


def test_follow_links_gates_link_sources():
    cfg = Config.from_dict({"links": {"follow_links": False, "youtube": True}})
    assert cfg.link_youtube is True
    assert cfg.link_source_enabled("youtube") is False


def test_legacy_quiz_values_convert_to_modes():
    # Legacy booleans, yes/no strings and mixed-case spellings map onto the
    # mode strings during conversion; the strict layer only accepts the
    # exact modes.
    for legacy, mode in (
        (True, "both"),
        (False, "off"),
        ("yes", "both"),
        ("none", "off"),
        ("HTML", "html"),
    ):
        converted = convert_legacy_config({"used_modules": {"url": {"quiz": legacy}}})
        assert converted["modules.quiz"] == mode
    # Unrecognized values pass through so validation reports them.
    converted = convert_legacy_config({"used_modules": {"url": {"quiz": "wat"}}})
    assert converted["modules.quiz"] == "wat"
    with pytest.raises(ConfigValidationError, match="modules.quiz must be one of"):
        validate_config(converted)


def test_quiz_mode_accepts_exact_modes():
    for mode in ("off", "html", "pdf", "both"):
        assert Config.from_dict({"modules": {"quiz": mode}}).quiz_mode == mode
    with pytest.raises(ConfigValidationError, match="modules.quiz must be one of"):
        validate_config({"modules": {"quiz": "HTML"}})


def test_quiz_defaults_to_html_when_no_modules_configured():
    # HTML is the safe default: it archives e-tests without launching a browser.
    assert Config.from_dict({}).quiz_mode == "html"


def test_partial_modules_table_keeps_defaults():
    cfg = Config.from_dict({"modules": {"assignment": False}})
    assert cfg.module_assignment is False
    assert cfg.module_resource is True
    assert cfg.module_folder is True
    assert cfg.link_youtube is True
    assert cfg.quiz_mode == "html"


def test_legacy_used_modules_tree_disables_omitted_entries():
    cfg = Config.from_dict(convert_legacy_config({"used_modules": {"assign": True}}))
    assert cfg.module_assignment is True
    assert cfg.module_resource is False
    assert cfg.module_folder is False
    assert cfg.link_youtube is False
    assert cfg.link_opencast is False
    assert cfg.link_sciebo is False
    assert cfg.quiz_mode == "off"
    # nolinks was a separate legacy toggle; the tree does not affect it.
    assert cfg.follow_links is True


def test_convert_legacy_config_does_not_mutate_input():
    raw = {"used_modules": {"url": {"quiz": True, "opencast": True}}}
    cfg = Config.from_dict(convert_legacy_config(raw))
    assert cfg.quiz_mode == "both"
    assert raw["used_modules"]["url"]["quiz"] is True


def test_module_helpers_reflect_legacy_toggles():
    cfg = Config.from_dict(
        convert_legacy_config(
            {
                "used_modules": {
                    "assign": False,
                    "folder": True,
                    "url": {"youtube": False, "sciebo": True},
                }
            }
        )
    )
    assert cfg.module_enabled("assignment") is False
    assert cfg.module_enabled("folder") is True
    assert cfg.link_source_enabled("youtube") is False
    assert cfg.link_source_enabled("sciebo") is True
    assert cfg.link_source_enabled("opencast") is False
    assert cfg.link_source_enabled("missing") is False
    assert cfg.module_enabled("missing") is False


def test_from_dict_accepts_none():
    cfg = Config.from_dict(None)
    assert cfg.sync_directory == "./"


def test_toml_example_is_valid():
    raw = tomllib.loads(Path("config.toml.example").read_text(encoding="utf-8"))
    validate_config(raw)
    cfg = Config.from_dict(raw)
    assert cfg.sync_directory == "./"
    assert cfg.course_prefix_handling == "suffix"
    assert cfg.update_files is True
    assert cfg.follow_links is True
    assert cfg.quiz_mode == "html"


def test_filter_values_are_normalized():
    cfg = Config.from_dict(
        {
            "courses.selected": 12,
            "filters.exclude_links": "*calendar*",
            "filters.allowed_domains": "moodle.rwth-aachen.de",
            "filters.exclude_sections": {"*": "General", 42: ["Hidden", None]},
            "filters.exclude_modules": {"42": "Quiz*"},
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

    assert cli.load_config(args, parser) == {
        "auth.user": "local-user",
        "auth.password": "global-password",
        "auth.totp_serial": "global-totp",
        "paths.sync_directory": "/local",
    }


def test_cli_loads_toml_configs_before_legacy_json(tmp_path, monkeypatch):
    xdg_config_dir = tmp_path / "xdg" / "syncmymoodle"
    xdg_config_dir.mkdir(parents=True)
    (xdg_config_dir / "config.json").write_text(
        json.dumps({"user": "global-json", "password": "json-password"}),
        encoding="utf-8",
    )
    (xdg_config_dir / "config.toml").write_text(
        '[auth]\nuser = "global-toml"\npassword = "toml-password"\ntotp_serial = "global-totp"\n',
        encoding="utf-8",
    )
    (tmp_path / "config.json").write_text(
        json.dumps({"user": "local-json", "basedir": "/json"}),
        encoding="utf-8",
    )
    (tmp_path / "config.toml").write_text(
        '[auth]\nuser = "local-toml"\n\n[paths]\nsync_directory = "/toml"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)

    parser = cli.build_parser()
    args = parser.parse_args([])

    assert cli.load_config(args, parser) == {
        "auth.user": "local-toml",
        "auth.password": "toml-password",
        "auth.totp_serial": "global-totp",
        "paths.sync_directory": "/toml",
    }


def test_local_config_overrides_global_across_key_spellings(tmp_path, monkeypatch):
    # Regression test: a global legacy JSON must not shadow a local TOML that
    # spells the same option differently (both resolve to one canonical key).
    xdg_config = tmp_path / "xdg" / "syncmymoodle" / "config.json"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text(
        json.dumps({"user": "u", "updatefiles": False, "nolinks": True}),
        encoding="utf-8",
    )
    (tmp_path / "config.toml").write_text(
        "[downloads]\nupdate_files = true\n\n[links]\nfollow_links = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)

    parser = cli.build_parser()
    args = parser.parse_args([])
    cfg = Config.from_dict(cli.load_config(args, parser))

    assert cfg.update_files is True
    assert cfg.follow_links is True


def test_cli_rejects_legacy_keys_in_toml_config(tmp_path, capsys):
    # Legacy spellings are only converted for JSON configs; TOML must use
    # the current names, and the error points at them.
    config_path = tmp_path / "sync.toml"
    config_path.write_text(
        """
user = "toml-user"

[used_modules.url]
quiz = "pdf"
""",
        encoding="utf-8",
    )

    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.load_config(args, parser)

    assert exc_info.value.code == 2
    error_output = capsys.readouterr().err
    assert "'user' is a legacy config key; use 'auth.user' instead" in error_output
    assert "'used_modules' is a legacy config key" in error_output


def test_cli_loads_grouped_toml_config(tmp_path):
    config_path = tmp_path / "sync.toml"
    config_path.write_text(
        """
[auth]
user = "toml-user"
password = "toml-password"
totp_serial = "toml-totp"

[paths]
sync_directory = "/tmp/moodle"

[courses]
prefix_handling = "suffix"

[downloads]
update_files = true

[filters]
allowed_domains = ["moodle.rwth-aachen.de"]
exclude_sections = ["General"]

[links]
youtube = false

[modules]
quiz = "pdf"
""",
        encoding="utf-8",
    )

    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    assert cli.load_config(args, parser) == {
        "auth.user": "toml-user",
        "auth.password": "toml-password",
        "auth.totp_serial": "toml-totp",
        "paths.sync_directory": "/tmp/moodle",
        "courses.prefix_handling": "suffix",
        "downloads.update_files": True,
        "filters.allowed_domains": ["moodle.rwth-aachen.de"],
        "filters.exclude_sections": ["General"],
        "links.youtube": False,
        "modules.quiz": "pdf",
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
        "auth.user": "json-user",
        "auth.password": "json-password",
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

    assert cli.load_config(args, parser) == {"auth.user": "json-user"}
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
        "auth.user": "explicit-user",
        "auth.password": "explicit-password",
        "auth.totp_serial": "explicit-totp",
    }


def test_malformed_discovered_config_reports_clean_error(tmp_path, monkeypatch, capsys):
    # Regression test: a broken auto-discovered config must fail with a
    # parser error naming the file, not an unhandled traceback.
    (tmp_path / "config.toml").write_text('user = "u\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
    monkeypatch.chdir(tmp_path)

    parser = cli.build_parser()
    args = parser.parse_args([])

    with pytest.raises(SystemExit) as exc_info:
        cli.load_config(args, parser)

    assert exc_info.value.code == 2
    error_output = capsys.readouterr().err
    assert "could not parse config file" in error_output
    assert "config.toml" in error_output


def test_invalid_discovered_config_reports_file_path(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.toml").write_text(
        '[courses]\nprefix_handling = "later"\n', encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
    monkeypatch.chdir(tmp_path)

    parser = cli.build_parser()
    args = parser.parse_args([])

    with pytest.raises(SystemExit) as exc_info:
        cli.load_config(args, parser)

    assert exc_info.value.code == 2
    error_output = capsys.readouterr().err
    assert "invalid config" in error_output
    assert "config.toml" in error_output
    assert "courses.prefix_handling must be one of" in error_output


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
                "nolinks": True,
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
            "totp_serial": "json-totp",
        },
        "courses": {"selected": ["course-a", "course-b"]},
        "links": {
            "follow_links": False,
            "youtube": False,
            "opencast": False,
            "sciebo": False,
        },
        "modules": {
            "assignment": False,
            "resource": False,
            "folder": False,
            "quiz": "html",
        },
    }
    assert str(output_path) in capsys.readouterr().out


def test_config_migrate_drops_nulls_and_restricts_permissions(tmp_path):
    # Regression test: JSON null values must be dropped (TOML has no null)
    # and the output may hold credentials, so it must be private.
    input_path = tmp_path / "config.json"
    output_path = tmp_path / "config.toml"
    input_path.write_text(
        json.dumps({"user": "u", "totpsecret": None, "chromium_path": None}),
        encoding="utf-8",
    )

    cli.migrate_json_config(input_path, output_path)

    migrated = tomllib.loads(output_path.read_text(encoding="utf-8"))
    assert migrated == {"auth": {"user": "u"}}
    assert output_path.stat().st_mode & 0o777 == 0o600


def test_config_migrate_rejects_invalid_config(tmp_path, capsys):
    input_path = tmp_path / "config.json"
    output_path = tmp_path / "config.toml"
    input_path.write_text(
        json.dumps({"user": "u", "baseddir": "/tmp/moodle"}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
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

    assert exc_info.value.code == 2
    assert "unknown config key" in capsys.readouterr().err
    assert not output_path.exists()


def test_config_check_command_reports_valid_config(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[auth]
user = "user"
password = "password"
totp_serial = "totp"

[courses]
prefix_handling = "suffix"
""",
        encoding="utf-8",
    )

    cli.main(["config", "check", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert f"Config is valid: {config_path}" in captured.out
    assert captured.err == ""


def test_config_check_honors_global_config_flag(tmp_path, capsys):
    # Regression test: the check subcommand's own --config default must not
    # clobber a --config given before the subcommand.
    config_path = tmp_path / "config.toml"
    config_path.write_text('[auth]\nuser = "user"\n', encoding="utf-8")

    cli.main(["--config", str(config_path), "config", "check"])

    assert f"Config is valid: {config_path}" in capsys.readouterr().out


def test_config_check_command_reports_validation_errors(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[paths]
sync_directoy = "/tmp/moodle"

[courses]
prefix_handling = "later"
""",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["config", "check", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.out == ""
    assert f"Config is invalid: {config_path}" in captured.err
    assert "- unknown config key 'paths.sync_directoy'." in captured.err
    assert "Did you mean 'paths.sync_directory'?" in captured.err
    assert "- courses.prefix_handling must be one of" in captured.err


def test_cli_rejects_invalid_config_before_sync(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[auth]
user = "user"
password = "password"
totp_serial = "totp"

[courses]
prefix_handling = "later"
""",
        encoding="utf-8",
    )
    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.config_from_args(args, parser)

    assert exc_info.value.code == 2
    assert "courses.prefix_handling must be one of" in capsys.readouterr().err


def test_cli_overrides_are_applied_after_config():
    fake_keyring = object()
    parser = cli.build_parser(fake_keyring)
    args = parser.parse_args(
        [
            "--user",
            "cli-user",
            "--password",
            "cli-password",
            "--totp-serial",
            "cli-totp",
            "--totp-secret",
            "cli-totp-secret",
            "--cookie-file",
            "/tmp/session",
            "--courses",
            "course-a,course-b",
            "--skip-courses",
            "course-c,course-d",
            "--semesters",
            "25ws,26ss",
            "--sync-directory",
            "/tmp/moodle",
            "--browser",
            "/usr/bin/chromium",
            "--course-prefix-handling",
            "suffix",
            "--use-keyring",
            "--keyring-store-totp-secret",
            "--no-follow-links",
            "--exclude-filetypes",
            "pdf,mp4",
            "--exclude-files",
            "*.bak,*.tmp",
            "--exclude-links",
            "*calendar*,*hinge*",
            "--allowed-domains",
            "moodle.rwth-aachen.de,rwth-aachen.sciebo.de",
            "--exclude-sections",
            "General,Week 1",
            "--exclude-modules",
            "Quiz*,resource",
            "--quiz",
            "pdf",
            "--update-files",
            "--conflict-handling",
            "keep",
        ]
    )
    config = canonicalize(
        {
            "auth": {
                "user": "config-user",
                "password": "config-password",
                "totp_serial": "config-totp",
            },
            "links": {"opencast": True},
            "modules": {"folder": False, "quiz": "off"},
        }
    )

    cli.apply_cli_overrides(config, args)
    cfg = Config.from_dict(config)

    assert cfg.user == "cli-user"
    assert cfg.password == "cli-password"
    assert cfg.totp_serial == "cli-totp"
    assert cfg.totp_secret == "cli-totp-secret"
    assert cfg.cookie_file == "/tmp/session"
    assert cfg.selected_courses == ["course-a", "course-b"]
    assert cfg.skip_courses == ["course-c", "course-d"]
    assert cfg.only_sync_semester == ["25ws", "26ss"]
    assert cfg.sync_directory == "/tmp/moodle"
    assert cfg.browser == "/usr/bin/chromium"
    assert cfg.course_prefix_handling == "suffix"
    assert cfg.use_keyring is True
    assert cfg.keyring_store_totp_secret is True
    assert cfg.follow_links is False  # --no-follow-links
    assert cfg.exclude_filetypes == ["pdf", "mp4"]
    assert cfg.exclude_files == ["*.bak", "*.tmp"]
    assert cfg.exclude_links == {"*": ["*calendar*", "*hinge*"]}
    assert cfg.allowed_domains == {
        "*": ["moodle.rwth-aachen.de", "rwth-aachen.sciebo.de"]
    }
    assert cfg.exclude_sections == {"*": ["General", "Week 1"]}
    assert cfg.exclude_modules == {"*": ["Quiz*", "resource"]}
    assert cfg.quiz_mode == "pdf"  # CLI beats the config's "off"
    assert cfg.link_opencast is True  # the rest of the config survives
    assert cfg.module_folder is False
    assert cfg.update_files is True
    assert cfg.conflict_handling == "keep"


def test_quiz_cli_override_keeps_other_modules_enabled():
    # Regression test: --quiz must not disable every other module when the
    # config has no module settings at all.
    parser = cli.build_parser()
    args = parser.parse_args(["--quiz", "pdf"])
    config: dict = {}

    cli.apply_cli_overrides(config, args)
    cfg = Config.from_dict(config)

    assert cfg.quiz_mode == "pdf"
    assert cfg.module_enabled("assignment")
    assert cfg.module_enabled("resource")
    assert cfg.module_enabled("folder")
    assert cfg.link_source_enabled("opencast")


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
    config = Config.from_dict(
        {
            "auth": {
                "user": "user",
                "totp_serial": "totp-provider",
                "use_keyring": True,
                "keyring_store_totp_secret": True,
            }
        }
    )

    cli.resolve_keyring_credentials(config, {}, fake_keyring)

    assert config.password == "stored-password"
    assert config.totp_secret == "stored-totp-secret"
    assert calls == [
        ("syncmymoodle", "user"),
        ("syncmymoodle", "totp-provider"),
    ]


def test_cli_password_seeds_keyring_on_first_use():
    stored: dict = {}
    fake_keyring = SimpleNamespace(
        get_password=lambda service, name: stored.get((service, name)),
        set_password=lambda service, name, value: stored.__setitem__(
            (service, name), value
        ),
    )
    config = Config.from_dict(
        {"auth": {"user": "user", "totp_serial": "totp", "use_keyring": True}}
    )
    config.password = "cli-password"

    cli.resolve_keyring_credentials(config, {}, fake_keyring)

    assert stored[("syncmymoodle", "user")] == "cli-password"
    assert config.password == "cli-password"


def test_keyring_rejects_password_from_config_file(caplog):
    caplog.set_level(logging.CRITICAL, logger="syncmymoodle.cli")
    config = Config.from_dict(
        {"auth": {"user": "user", "password": "secret", "use_keyring": True}}
    )
    file_config = {"auth.password": "secret"}

    with pytest.raises(SystemExit) as exc_info:
        cli.resolve_keyring_credentials(config, file_config, object())

    assert exc_info.value.code == 1
    assert "remove your password" in caplog.text


def test_use_keyring_without_keyring_fails_clearly(tmp_path, caplog):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[auth]\nuser = "u"\ntotp_serial = "t"\nuse_keyring = true\n',
        encoding="utf-8",
    )
    caplog.set_level(logging.CRITICAL, logger="syncmymoodle.cli")

    parser = cli.build_parser(None)
    args = parser.parse_args(["--config", str(config_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.config_from_args(args, parser, None)

    assert exc_info.value.code == 1
    assert "keyring package is not installed" in caplog.text


def test_legacy_flat_json_keys_resolve_end_to_end(tmp_path, monkeypatch):
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

    assert captured["config"].follow_links is False
    assert captured["config"].update_files is True
