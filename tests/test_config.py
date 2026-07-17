import json
import logging
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit

import syncmymoodle.cli as cli
import syncmymoodle.secret_providers as secret_providers
from syncmymoodle.config import (
    CONFIG_OPTIONS,
    LEGACY_KEY_MAP,
    Config,
    ConfigValidationError,
    canonicalize,
    convert_legacy_config,
    group_config_for_toml,
)
from syncmymoodle.context import AuthState
from syncmymoodle.output import TerminalOutput

from .helpers import FakeKeyring, make_context


def validate_config(raw):
    Config.from_dict(raw)


def test_config_options_record_cli_overrides():
    assert {
        option.cli.arg_name: option.canonical_key
        for option in CONFIG_OPTIONS
        if option.cli
    } == {
        "user": "auth.user",
        "totp-serial": "auth.login.totp_serial",
        "keyring-store-totp-secret": "auth.login.keyring_store_totp_secret",
        "login-env-file": "auth.login.env_file",
        "sync-directory": "paths.sync_directory",
        "cookie-file": "paths.cookie_file",
        "browser": "paths.browser",
        "courses": "courses.selected",
        "skip-courses": "courses.skip",
        "exclude-course-roles": "courses.exclude_roles",
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
        "follow-links": "links.follow_links",
        "youtube": "links.youtube",
        "opencast": "links.opencast",
        "sciebo": "links.sciebo",
        "emedia": "links.emedia",
        "quiz": "modules.quiz",
    }


def test_deprecated_cli_flag_spellings_warn_and_still_work(capsys):
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
    warnings = capsys.readouterr().err
    assert "--skipcourses is deprecated; use --skip-courses instead" in warnings
    assert "--nolinks is deprecated; use --no-follow-links instead" in warnings
    assert (
        "--updatefilesconflict is deprecated; use --conflict-handling instead"
        in warnings
    )
    # The deprecated spellings remain hidden from --help.
    help_text = parser.format_help()
    assert "--skip-courses" in help_text
    assert "--skipcourses" not in help_text


def test_cli_help_groups_config_options():
    help_text = cli.build_parser().format_help()

    assert help_text.startswith("usage: syncmymoodle")
    assert "python3 -m syncmymoodle" not in help_text
    assert "\nauth:\n" in help_text
    assert "\npaths:\n" in help_text
    assert "\ncourses:\n" in help_text
    assert "\ndownloads:\n" in help_text
    assert "\nfilters:\n" in help_text
    assert "\nlinks:\n" in help_text
    assert "\nmodules:\n" in help_text
    assert help_text.index("\nauth:\n") < help_text.index("  --user USER")
    links_group = help_text.index("\nlinks:\n")
    assert links_group < help_text.index("  --follow-links", links_group)
    assert "--password" not in help_text
    assert "--totp-secret" not in help_text
    assert "25ws,26ss" in help_text
    assert "Defaults to None" not in help_text
    assert "set the directory to sync Moodle files to" in help_text
    assert "Run without a subcommand to sync RWTH Moodle" in help_text
    assert "--update-files, --no-update-files" in help_text
    assert "Moodle course URLs or" in help_text
    assert "numeric IDs" in help_text
    assert "one of your directly assigned" in help_text
    assert "Moodle course roles has" in help_text
    assert "whose size is known" in help_text
    assert "--show-filtered" in help_text
    assert "--color {auto,always,never}" in help_text


def test_filtered_report_is_summarized_by_default(capsys):
    ctx = make_context()
    ctx.record_filtered(
        "filters.exclude_files",
        "file",
        "/sync/Course/notes.tmp",
        "matches '*.tmp'",
    )

    cli.report_filtered_items(ctx, show_details=False)

    assert capsys.readouterr().out == (
        "Filtered 1 item; use --show-filtered for details.\n"
    )


def test_show_filtered_report_is_grouped_and_deduplicated(capsys):
    ctx = make_context()
    ctx.record_filtered(
        "filters.exclude_files",
        "file",
        "/sync/Course/notes.tmp",
        "matches '*.tmp'",
    )
    ctx.record_filtered(
        "courses.skip",
        "course",
        "Skipped Course (123)",
        "matches '123'",
    )
    ctx.record_filtered(
        "filters.exclude_files",
        "file",
        "/sync/Course/notes.tmp",
        "matches '*.tmp'",
    )

    cli.report_filtered_items(ctx, show_details=True)

    assert capsys.readouterr().out == (
        "Filtered items (2):\n"
        "  courses.skip (1):\n"
        "    course: Skipped Course (123) - matches '123'\n"
        "  filters.exclude_files (1):\n"
        "    file: /sync/Course/notes.tmp - matches '*.tmp'\n"
    )


def test_filtered_report_is_colored_without_parsing_item_markup(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    ctx = make_context()
    ctx.output = TerminalOutput("always")
    ctx.record_filtered(
        "filters.exclude_files",
        "file",
        "/sync/[red]notes.tmp[/]",
        "matches '[red]*.tmp[/]'",
    )

    cli.report_filtered_items(ctx, show_details=True)

    output = capsys.readouterr().out
    assert "\x1b[" in output
    assert "[red]notes.tmp[/]" in output
    assert "matches '[red]*.tmp[/]'" in output


def test_show_filtered_is_forwarded_to_sync_run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))
    monkeypatch.setattr(
        cli,
        "run",
        lambda ctx, *, show_filtered=False: calls.append(show_filtered),
    )

    cli.main(["--sync-directory", str(tmp_path), "--show-filtered"])

    assert calls == [True]


def test_partial_sync_failure_exits_nonzero_after_run(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))

    def fail_one_item(ctx, *, show_filtered=False):
        del show_filtered
        ctx.stats.failed = 1

    monkeypatch.setattr(cli, "run", fail_one_item)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--sync-directory", str(tmp_path), "--color", "never"])

    assert exc_info.value.code == 1


def test_keyboard_interrupt_exits_cleanly_with_shell_status(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)

    def interrupt(args, parser):
        del args, parser
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_config_command", interrupt)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--color", "always", "config", "example"])

    assert exc_info.value.code == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "\x1b[31mInterrupted.\x1b[0m\n"


def test_setup_help_explains_what_will_be_configured(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["setup", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "usage: syncmymoodle setup" in output
    assert "configure RWTH sign-in, secure Moodle token storage" in output
    assert "verify the RWTH sign-in with one login" in output


@pytest.mark.parametrize(
    ("argv", "option"),
    [
        (["--user", "user", "setup"], "--user"),
        (["--courses", "123", "config", "example"], "--courses"),
        (["--no-update-files", "clean", "conflicts"], "--update-files"),
        (["--show-filtered", "config", "example"], "--show-filtered"),
    ],
)
def test_management_commands_reject_sync_options(argv, option, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv)

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "sync options cannot be used" in error
    assert option in error


def test_totp_manual_is_scoped_to_auth_login(capsys):
    parser = cli.build_parser()

    args = parser.parse_args(["auth", "login", "--totp-manual"])

    assert args.totp_manual is True
    assert "--totp-manual" not in parser.format_help()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["auth", "status", "--totp-manual"])
    assert exc_info.value.code == 2
    assert "unrecognized arguments: --totp-manual" in capsys.readouterr().err


def test_config_selection_is_rejected_when_command_does_not_read_it(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--config", "unused.toml", "config", "example"])

    assert exc_info.value.code == 2
    assert "--config is only supported with `config check`" in capsys.readouterr().err


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])

    assert exc_info.value.code == 0
    assert "syncmymoodle" in capsys.readouterr().out


def test_parse_errors_use_requested_color_and_keep_usage_plain(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--color", "always", "--unknown-option"])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert error.startswith("usage: syncmymoodle ")
    assert "\x1b[31msyncmymoodle: error: " in error
    assert "unrecognized arguments: --unknown-option" in error


def test_plain_run_without_config_points_to_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    with pytest.raises(SystemExit) as exc_info:
        cli.main([])

    assert exc_info.value.code == 2
    assert "syncmymoodle setup" in capsys.readouterr().err


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
    with pytest.raises(
        ConfigValidationError, match="filters.max_file_size must be a size"
    ):
        validate_config({"filters": {"max_file_size": False}})


def test_file_size_validation_rejects_values_too_large_to_represent():
    with pytest.raises(
        ConfigValidationError,
        match="filters.max_file_size must be a size",
    ):
        validate_config({"filters": {"max_file_size": f"{'9' * 400}T"}})


def test_config_validation_rejects_inverted_size_limits():
    with pytest.raises(
        ConfigValidationError,
        match="filters.min_file_size must not exceed filters.max_file_size",
    ):
        validate_config({"filters": {"min_file_size": "2M", "max_file_size": "1M"}})


def test_config_validation_rejects_active_managed_file_collisions(tmp_path):
    shared = str(tmp_path / "shared")
    conflicting_configs = [
        {
            "auth": {
                "tokens": {"store": "env-file", "env_file": shared},
                "login": {"provider": "env-file", "env_file": shared},
            }
        },
        {
            "auth": {"tokens": {"store": "env-file", "env_file": shared}},
            "paths": {"cookie_file": shared},
        },
        {
            "auth": {"login": {"provider": "env-file", "env_file": shared}},
            "paths": {"cookie_file": shared},
        },
    ]

    for raw in conflicting_configs:
        with pytest.raises(ConfigValidationError, match="configure separate files"):
            validate_config(raw)


def test_config_validation_rejects_auth_store_aliasing_config_file(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[auth.tokens]
store = "env-file"
env_file = "config.toml"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="configuration file"):
        cli.read_config_file(config_path)


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
            "auth": {
                "user": "user",
                "tokens": {"store": "keyring"},
                "login": {
                    "provider": "pass",
                    "password": "rwth/moodle",
                },
            },
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


@pytest.mark.parametrize(
    "config_text",
    [
        '"auth.user" = "user"\n',
        '["auth.login"]\nprovider = "pass"\npassword = "rwth"\n',
        '[auth]\n"login.provider" = "pass"\n"login.password" = "rwth"\n',
    ],
)
def test_toml_config_rejects_literal_dotted_schema_keys(tmp_path, config_text):
    config_path = tmp_path / "config.toml"
    config_path.write_text(config_text, encoding="utf-8")

    with pytest.raises(ConfigValidationError, match="literal dotted TOML key"):
        cli.read_config_file(config_path)


def test_toml_grouping_nests_dotted_schema_groups():
    grouped = group_config_for_toml(
        {
            "auth.user": "user",
            "auth.login.provider": "pass",
            "auth.login.password": "rwth",
        }
    )

    assert grouped == {
        "auth": {
            "user": "user",
            "login": {"provider": "pass", "password": "rwth"},
        }
    }
    text = tomlkit.dumps(grouped)
    assert "[auth.login]" in text
    assert '["auth.login"]' not in text


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
                "links": {"follow_links": "false", "youtube": "false"},
                "modules": {"folder": "true"},
            }
        )

    message = str(exc_info.value)
    assert "links.follow_links must be true or false" in message
    assert "modules.folder must be true or false" in message
    assert "links.youtube must be true or false" in message


@pytest.mark.parametrize("key", ["auto_reauthenticate", "totp_manual"])
def test_removed_login_policy_settings_are_rejected(key):
    with pytest.raises(ConfigValidationError, match=f"auth.login.{key}"):
        validate_config({"auth": {"login": {key: True}}})


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


def test_defaults_applied_for_empty_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    cfg = Config.from_dict({})
    assert cfg.sync_directory == "./"
    assert cfg.cookie_file == str(tmp_path / "xdg" / "syncmymoodle" / "session")
    assert cfg.course_prefix_handling == "keep"
    assert cfg.conflict_handling == "rename"
    assert cfg.follow_links is True
    assert cfg.update_files is False
    assert cfg.selected_courses == []
    assert cfg.exclude_course_roles == []
    assert cfg.exclude_links == {}
    # Default module and link toggles are on.
    assert cfg.module_assignment
    assert cfg.module_folder
    assert cfg.link_source_enabled("opencast")
    assert cfg.link_source_enabled("emedia")
    # HTML is the safe quiz default: it archives attempts without a browser.
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


def test_legacy_none_conflict_mode_preserves_keep_behavior():
    converted = convert_legacy_config({"update_files_conflict": "none"})

    assert converted["downloads.conflict_handling"] == "keep"
    assert Config.from_dict(converted).conflict_handling == "keep"


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
    assert cfg.link_emedia is True
    assert cfg.quiz_mode == "off"
    # nolinks was a separate legacy toggle; the tree does not affect it.
    assert cfg.follow_links is True


def test_empty_legacy_used_modules_tree_keeps_historical_defaults():
    cfg = Config.from_dict(convert_legacy_config({"used_modules": {}}))

    assert cfg.module_assignment is True
    assert cfg.module_resource is True
    assert cfg.module_folder is True
    assert cfg.link_youtube is True
    assert cfg.link_opencast is True
    assert cfg.link_sciebo is True
    assert cfg.link_emedia is True
    assert cfg.quiz_mode == "html"


def test_convert_legacy_config_does_not_mutate_input():
    raw = {"used_modules": {"url": {"quiz": True, "opencast": True}}}
    cfg = Config.from_dict(convert_legacy_config(raw))
    assert cfg.quiz_mode == "both"
    assert raw["used_modules"]["url"]["quiz"] is True


def test_module_and_link_flags_reflect_legacy_toggles():
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
    assert cfg.module_assignment is False
    assert cfg.module_folder is True
    assert cfg.link_source_enabled("youtube") is False
    assert cfg.link_source_enabled("sciebo") is True
    assert cfg.link_source_enabled("opencast") is False
    assert cfg.link_source_enabled("missing") is False


def test_toml_example_is_valid():
    example_text = cli.starter_config_text()
    raw = tomllib.loads(example_text)
    validate_config(raw)
    # The opt-in keyring TOTP seed setting is documented as a commented example.
    assert set(canonicalize(raw)) == {
        option.canonical_key
        for option in CONFIG_OPTIONS
        if option.canonical_key != "auth.login.keyring_store_totp_secret"
    }
    cfg = Config.from_dict(raw)
    assert raw["paths"]["sync_directory"] == ""
    assert "password" not in raw["auth"]
    assert "totp_secret" not in raw["auth"]
    assert cfg.sync_directory == "./"
    assert cfg.course_prefix_handling == "suffix"
    assert cfg.update_files is True
    assert cfg.follow_links is True
    assert cfg.quiz_mode == "html"


def test_migrated_config_text_only_adds_behavior_compatible_defaults():
    baseline = Config.from_dict({})

    migrated_text = cli.migrated_config_text({}, baseline)
    migrated = tomllib.loads(migrated_text)
    migrated_values = canonicalize(migrated)

    assert Config.from_dict(migrated) == baseline
    assert migrated_values["downloads.conflict_handling"] == "rename"
    assert migrated_values["downloads.dry_run"] is False
    assert migrated_values["links.follow_links"] is True
    assert migrated_values["modules.quiz"] == "html"
    assert {
        "auth.user",
        "auth.login.totp_serial",
        "courses.prefix_handling",
        "downloads.update_files",
    }.isdisjoint(migrated_values)
    assert "# Relative paths in this file resolve" in migrated_text


def test_migrated_config_text_keeps_explicit_non_default_values():
    values = {
        "courses.prefix_handling": "remove",
        "downloads.update_files": True,
    }

    migrated = canonicalize(
        tomllib.loads(cli.migrated_config_text(values, Config.from_dict(values)))
    )

    assert migrated["courses.prefix_handling"] == "remove"
    assert migrated["downloads.update_files"] is True


def test_current_config_parser_requires_toml_content(tmp_path):
    json_named_toml = tmp_path / "config.json"
    json_named_toml.write_text('[auth]\nuser = "toml-user"\n', encoding="utf-8")
    toml_named_json = tmp_path / "config.toml"
    toml_named_json.write_text('{"user": "json-user"}', encoding="utf-8")

    assert cli.read_config_file(json_named_toml) == {"auth.user": "toml-user"}
    with pytest.raises(ValueError, match="syncmymoodle config migrate --input"):
        cli.read_config_file(toml_named_json)


def test_filter_values_are_normalized():
    cfg = Config.from_dict(
        {
            "courses.selected": 12,
            "courses.exclude_roles": ["Tutor", " tutor ", ""],
            "filters.exclude_links": "*calendar*",
            "filters.allowed_domains": "moodle.rwth-aachen.de",
            "filters.exclude_sections": {"*": "General", 42: ["Hidden", None]},
            "filters.exclude_modules": {"42": "Quiz*"},
        }
    )

    assert cfg.selected_courses == ["12"]
    assert cfg.exclude_course_roles == ["tutor"]
    assert cfg.matching_excluded_course_role({" Student ", "TUTOR"}) == "tutor"
    assert cfg.matching_excluded_course_role(None) is None
    assert cfg.exclude_links == {"*": ["*calendar*"]}
    assert cfg.allowed_domains == {"*": ["moodle.rwth-aachen.de"]}
    assert cfg.exclude_sections == {"*": ["General"], "42": ["Hidden"]}
    assert cfg.exclude_modules == {"42": ["Quiz*"]}


def test_cli_ignores_cwd_config_by_default(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        json.dumps({"user": "local-user", "basedir": "/local"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))
    monkeypatch.chdir(tmp_path)

    parser = cli.build_parser()
    args = parser.parse_args([])

    assert cli.load_config(args, parser) == {}


def test_cli_prefers_global_toml_config_over_global_legacy_json(tmp_path, monkeypatch):
    xdg_config_dir = tmp_path / "xdg" / "syncmymoodle"
    xdg_config_dir.mkdir(parents=True)
    (xdg_config_dir / "config.json").write_text(
        json.dumps({"user": "global-json", "password": "json-password"}),
        encoding="utf-8",
    )
    (xdg_config_dir / "config.toml").write_text(
        '[auth]\nuser = "global-toml"\n\n[auth.login]\ntotp_serial = "global-totp"\n',
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
        "auth.user": "global-toml",
        "auth.login.totp_serial": "global-totp",
    }


def test_explicit_config_can_read_cwd_config(tmp_path, monkeypatch):
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
    args = parser.parse_args(["--config", "config.toml"])
    cfg = Config.from_dict(cli.load_config(args, parser))

    assert cfg.update_files is True
    assert cfg.follow_links is True


def test_explicit_config_relative_paths_resolve_from_config_dir(tmp_path, monkeypatch):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    config_path = config_dir / "sync.toml"
    config_path.write_text(
        """
[paths]
sync_directory = "downloads"
cookie_file = "cookies/session"
browser = "bin/chrome"

[auth.login]
env_file = "secrets.env"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(cwd)

    parser = cli.build_parser()
    args = parser.parse_args(["--config", "../configs/sync.toml"])

    assert cli.load_config(args, parser) == {
        "paths.sync_directory": str(config_dir / "downloads"),
        "paths.cookie_file": str(config_dir / "cookies" / "session"),
        "paths.browser": str(config_dir / "bin" / "chrome"),
        "auth.login.env_file": str(config_dir / "secrets.env"),
    }


def test_global_config_relative_paths_resolve_from_global_config_dir(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "xdg" / "syncmymoodle"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[paths]
sync_directory = "downloads"
cookie_file = "cookies/session"
browser = "bin/chrome"

[auth.login]
env_file = "secrets.env"
""",
        encoding="utf-8",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    parser = cli.build_parser()
    args = parser.parse_args([])

    assert cli.load_config(args, parser) == {
        "paths.sync_directory": str(config_dir / "downloads"),
        "paths.cookie_file": str(config_dir / "cookies" / "session"),
        "paths.browser": str(config_dir / "bin" / "chrome"),
        "auth.login.env_file": str(config_dir / "secrets.env"),
    }


def test_cli_relative_path_overrides_resolve_from_invoking_cwd(tmp_path, monkeypatch):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    (config_dir / "sync.toml").write_text(
        """
[paths]
sync_directory = "config-downloads"
cookie_file = "config-session"
browser = "config-browser"

[auth.login]
env_file = "config.env"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(cwd)

    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--config",
            "../configs/sync.toml",
            "--sync-directory",
            "cli-downloads",
            "--cookie-file",
            "cli-session",
            "--browser",
            "bin/chrome",
            "--login-env-file",
            "cli.env",
        ]
    )

    ctx = cli.context_from_args(args, parser)
    config = ctx.config

    assert config.sync_directory == str(cwd / "cli-downloads")
    assert config.cookie_file == str(cwd / "cli-session")
    assert config.browser == str(cwd / "bin" / "chrome")
    assert config.login_env_file == str(cwd / "cli.env")


def test_cli_path_overrides_do_not_resolve_symlinks(tmp_path, monkeypatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    real_browser = tmp_path / "real-chrome"
    real_browser.touch()
    browser = cwd / "chrome"
    try:
        browser.symlink_to(real_browser)
    except OSError as exc:
        pytest.skip(f"symlinks are not available: {exc}")
    monkeypatch.chdir(cwd)

    parser = cli.build_parser()
    args = parser.parse_args(["--browser", "chrome"])
    config: dict = {}

    cli.apply_cli_overrides(config, args)

    assert config == {"paths.browser": str(browser)}


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

[auth.login]
totp_serial = "toml-totp"

[paths]
sync_directory = "moodle"

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
        "auth.login.totp_serial": "toml-totp",
        "paths.sync_directory": str(tmp_path / "moodle"),
        "courses.prefix_handling": "suffix",
        "downloads.update_files": True,
        "filters.allowed_domains": ["moodle.rwth-aachen.de"],
        "filters.exclude_sections": ["General"],
        "links.youtube": False,
        "modules.quiz": "pdf",
    }


def test_cli_requires_migration_for_global_legacy_json(tmp_path, monkeypatch, capsys):
    xdg_config = tmp_path / "xdg" / "syncmymoodle" / "config.json"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text(
        json.dumps({"user": "json-user"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--color", "always"])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert str(xdg_config) in error
    assert "syncmymoodle config migrate" in error
    assert "\x1b[31msyncmymoodle: error: found legacy JSON config" in error


def test_cli_explicit_config_skips_discovery(tmp_path, monkeypatch):
    xdg_config = tmp_path / "xdg" / "syncmymoodle" / "config.json"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text(json.dumps({"user": "global-user"}), encoding="utf-8")
    (tmp_path / "config.json").write_text(
        json.dumps({"user": "local-user"}), encoding="utf-8"
    )
    explicit_config = tmp_path / "chosen.toml"
    explicit_config.write_text(
        '[auth]\nuser = "explicit-user"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)

    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(explicit_config)])

    assert cli.load_config(args, parser) == {"auth.user": "explicit-user"}


def test_malformed_discovered_config_reports_clean_error(tmp_path, monkeypatch, capsys):
    # Regression test: a broken auto-discovered config must fail with a
    # parser error naming the file, not an unhandled traceback.
    xdg_config = tmp_path / "xdg" / "syncmymoodle" / "config.toml"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text('user = "u\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
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
    xdg_config = tmp_path / "xdg" / "syncmymoodle" / "config.toml"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text('[courses]\nprefix_handling = "later"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
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


def test_config_migrate_command_writes_secret_free_toml_and_tokens(
    tmp_path, monkeypatch, capsys
):
    input_path = tmp_path / "config.json"
    output_path = tmp_path / "config.toml"
    token_path = tmp_path / "mobile-token.env"
    input_path.write_text(
        json.dumps(
            {
                "user": "json-user",
                "password": "json-password",
                "totp": "json-totp",
                "totpsecret": "json-totp-secret",
                "chromium_path": None,
                "selected_courses": ["course-a", "course-b"],
                "nolinks": True,
                "used_modules": {"url": {"quiz": "html"}},
            }
        ),
        encoding="utf-8",
    )
    tokens = cli.MoodleTokens(
        "json-user", "ws-token", "private-token", moodle_user_id=123
    )

    def fake_login(ctx, log, *, reuse_cached_session):
        assert reuse_cached_session is False
        assert ctx.auth.password == "json-password"
        assert ctx.auth.totp_secret == "json-totp-secret"

    monkeypatch.setattr(cli.rwth, "login", fake_login)
    monkeypatch.setattr(
        cli, "acquire_validated_moodle_tokens", lambda ctx, parser: tokens
    )
    input_reads = 0
    original_read_text = Path.read_text

    def count_input_reads(path, *args, **kwargs):
        nonlocal input_reads
        if path == input_path:
            input_reads += 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", count_input_reads)

    cli.main(
        [
            "config",
            "migrate",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--token-store",
            "env-file",
            "--token-env-file",
            str(token_path),
        ]
    )

    assert input_reads == 1
    migrated_text = output_path.read_text(encoding="utf-8")
    migrated = tomllib.loads(migrated_text)
    explicitly_migrated = {
        "auth": {
            "user": "json-user",
            "tokens": {"store": "env-file", "env_file": str(token_path)},
            "login": {
                "provider": "prompt",
                "totp_serial": "json-totp",
            },
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
    assert Config.from_dict(migrated) == Config.from_dict(explicitly_migrated)
    migrated_values = canonicalize(migrated)
    assert migrated_values["downloads.conflict_handling"] == "rename"
    assert migrated_values["downloads.dry_run"] is False
    assert migrated_values["links.emedia"] is True
    assert "courses.prefix_handling" not in migrated_values
    assert "downloads.update_files" not in migrated_values
    assert "# Relative paths in this file resolve" in migrated_text
    if os.name != "nt":
        assert output_path.stat().st_mode & 0o777 == 0o600
    assert "json-password" not in migrated_text
    assert cli.EnvFileTokenStore(token_path, "json-user").load() == tokens
    assert json.loads(input_path.read_text(encoding="utf-8"))["password"] == (
        "json-password"
    )
    captured = capsys.readouterr()
    assert str(output_path) in captured.out
    assert "source JSON was left unchanged and still contains secrets" in captured.err
    assert (
        "Review the migrated TOML and source JSON, then delete the source JSON"
        in captured.out
    )


@pytest.mark.parametrize("destination", ["output", "cookie", "token"])
def test_config_migrate_rejects_source_as_managed_destination(
    tmp_path, monkeypatch, capsys, destination
):
    input_path = tmp_path / "config.json"
    output_path = input_path if destination == "output" else tmp_path / "config.toml"
    token_path = input_path if destination == "token" else tmp_path / "tokens.env"
    legacy = {"user": "json-user", "password": "json-password"}
    if destination == "cookie":
        legacy["cookie_file"] = str(input_path)
    source_text = json.dumps(legacy)
    input_path.write_text(source_text, encoding="utf-8")
    login_called = False

    def fake_login(*args, **kwargs):
        nonlocal login_called
        login_called = True

    monkeypatch.setattr(cli.rwth, "login", fake_login)

    args = [
        "config",
        "migrate",
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--token-store",
        "env-file",
        "--token-env-file",
        str(token_path),
    ]
    if destination == "output":
        args.append("--force")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(args)

    assert exc_info.value.code == 2
    assert not login_called
    assert input_path.read_text(encoding="utf-8") == source_text
    if output_path != input_path:
        assert not output_path.exists()
    assert "same path as migration input" in capsys.readouterr().err


def test_config_migrate_removes_new_token_if_config_write_fails(tmp_path, monkeypatch):
    input_path = tmp_path / "config.json"
    output_path = tmp_path / "config.toml"
    token_path = tmp_path / "mobile-token.env"
    input_path.write_text(
        json.dumps({"user": "json-user", "password": "json-password"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.rwth, "login", lambda ctx, log, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "acquire_validated_moodle_tokens",
        lambda ctx, parser: cli.MoodleTokens(
            "json-user", "ws-token", "private-token", moodle_user_id=123
        ),
    )

    def fail_config_write(*args):
        raise PermissionError("read-only config")

    monkeypatch.setattr(cli, "write_private_text", fail_config_write)

    with pytest.raises(SystemExit):
        cli.main(
            [
                "config",
                "migrate",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--token-store",
                "env-file",
                "--token-env-file",
                str(token_path),
            ]
        )

    assert not output_path.exists()
    assert not token_path.exists()


def test_config_example_prints_template_without_modifying_global_config(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "xdg" / "syncmymoodle" / "config.toml"
    config_path.parent.mkdir(parents=True)
    existing = '[auth]\nuser = "existing"\n'
    config_path.write_text(existing, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(
        "builtins.input", lambda: pytest.fail("example must not prompt")
    )

    cli.main(["config", "example"])

    output = capsys.readouterr().out
    assert output == cli.starter_config_text()
    validate_config(tomllib.loads(output))
    assert config_path.read_text(encoding="utf-8") == existing


def test_config_path_command_prints_global_config_paths(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "xdg" / "syncmymoodle"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    config_path.write_text('[auth]\nuser = "u"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    cli.main(["config", "path"])

    output = capsys.readouterr().out
    assert f"Global config directory: {config_dir}" in output
    assert f"Default TOML config: {config_path}" in output
    assert f"Discovered config: {config_path}" in output


def test_config_migrate_default_input_ignores_cwd_config(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.json").write_text(
        json.dumps({"user": "local"}), encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["config", "migrate"])

    assert exc_info.value.code == 2
    assert "no legacy config.json found" in capsys.readouterr().err


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


def test_config_migrate_rejects_toml_input_by_content(tmp_path, capsys):
    input_path = tmp_path / "config.json"
    output_path = tmp_path / "config.toml"
    input_path.write_text('[auth]\nuser = "toml-user"\n', encoding="utf-8")

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
    assert "legacy JSON config file" in capsys.readouterr().err
    assert not output_path.exists()


def test_config_check_command_reports_valid_config(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[auth]
user = "user"

[auth.login]
totp_serial = "totp"

[courses]
prefix_handling = "suffix"
""",
        encoding="utf-8",
    )

    cli.main(["--config", str(config_path), "config", "check"])

    captured = capsys.readouterr()
    assert f"Config is valid: {config_path}" in captured.out
    assert captured.err == ""


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
        cli.main(["--config", str(config_path), "config", "check"])

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

[auth.login]
totp_serial = "totp"

[courses]
prefix_handling = "later"
""",
        encoding="utf-8",
    )
    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.context_from_args(args, parser)

    assert exc_info.value.code == 2
    assert "courses.prefix_handling must be one of" in capsys.readouterr().err


def test_cli_rejects_invalid_cli_overrides_without_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--user",
            "user",
            "--totp-serial",
            "totp",
            "--max-file-size",
            "huge",
        ]
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.context_from_args(args, parser)

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "filters.max_file_size must be a size" in captured.err
    assert "Traceback" not in captured.err


def test_cli_allows_prompted_password_flow_without_stored_password(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[auth]
user = "user"

[auth.login]
totp_serial = "totp"
""",
        encoding="utf-8",
    )
    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    ctx = cli.context_from_args(args, parser)
    config = ctx.config

    assert config.user == "user"
    assert config.login_provider == "prompt"
    assert config.totp_serial == "totp"
    assert ctx.auth.password is None


def test_cli_reads_credentials_from_env_file(tmp_path, monkeypatch):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "secrets.env").write_text(
        """
SYNCMYMOODLE_PASSWORD=env-password
SYNCMYMOODLE_TOTP_SECRET="env-totp-secret"
""",
        encoding="utf-8",
    )
    config_path = config_dir / "config.toml"
    config_path.write_text(
        """
[auth]
user = "user"

[auth.login]
provider = "env-file"
totp_serial = "totp"
env_file = "secrets.env"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    reads = []
    real_read_env_file = secret_providers.read_env_file

    def record_read(path):
        reads.append(path)
        return real_read_env_file(path)

    monkeypatch.setattr(secret_providers, "read_env_file", record_read)

    parser = cli.build_parser()
    args = parser.parse_args(["--config", "configs/config.toml"])

    ctx = cli.context_from_args(args, parser)
    config = ctx.config

    assert config.login_env_file == str(config_dir / "secrets.env")
    assert ctx.auth.credential_resolver is not None
    ctx.auth.credential_resolver()
    assert ctx.auth.password == "env-password"
    assert ctx.auth.totp_secret == "env-totp-secret"
    assert reads == [config_dir / "secrets.env"]


def test_auth_login_totp_manual_skips_env_file_totp_secret(tmp_path):
    env_path = tmp_path / "secrets.env"
    env_path.write_text(
        """
SYNCMYMOODLE_PASSWORD=env-password
SYNCMYMOODLE_TOTP_SECRET=env-totp-secret
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[auth]
user = "user"

[auth.login]
provider = "env-file"
totp_serial = "totp"
env_file = {str(env_path)!r}
""",
        encoding="utf-8",
    )
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--config", str(config_path), "auth", "login", "--totp-manual"]
    )

    ctx = cli.configured_auth_context(args, parser, None)

    assert ctx.auth.credential_resolver is not None
    ctx.auth.credential_resolver()
    assert ctx.auth.password == "env-password"
    assert ctx.auth.totp_secret is None


EXTERNAL_PROVIDER_CONFIG = """
[auth]
user = "user"

[auth.login]
provider = "1password"
totp_serial = "totp"
password = "op://Private/RWTH/password"
otp = "op://Private/RWTH/otp?attribute=otp"
"""
COMMAND_PROVIDER_CONFIG = """
[auth]
user = "user"

[auth.login]
provider = "command"
totp_serial = "totp"
password_command = ["secret-tool", "lookup", "rwth"]
otp_command = ["otp-tool", "code", "rwth"]
"""


def _write_external_provider_config(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(EXTERNAL_PROVIDER_CONFIG, encoding="utf-8")
    return config_path


def _install_external_provider(monkeypatch):
    otp_calls = []

    class FakeProvider:
        def check_available(self):
            return SimpleNamespace(available=True, reason=None)

        def get_password(self, reference):
            assert reference == "op://Private/RWTH/password"
            return "provider-password"

        def get_otp_code(self, reference):
            assert reference == "op://Private/RWTH/otp?attribute=otp"
            otp_calls.append(reference)
            return "123456"

    provider = FakeProvider()
    monkeypatch.setattr(
        cli,
        "build_external_secret_provider",
        lambda provider_name: provider,
    )
    return otp_calls


def _install_command_provider(tmp_path, monkeypatch):
    config_dir = tmp_path / "xdg" / "syncmymoodle"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(COMMAND_PROVIDER_CONFIG, encoding="utf-8")
    otp_events = []

    class FakeProvider:
        def __init__(self, password_command, otp_command):
            assert password_command == ("secret-tool", "lookup", "rwth")
            assert otp_command == ("otp-tool", "code", "rwth")

        def check_available(self):
            return SimpleNamespace(available=True, reason=None)

        def check_otp_available(self):
            otp_events.append("availability")
            return SimpleNamespace(available=True, reason=None)

        def get_password(self, reference):
            assert reference == ""
            return "command-password"

        def get_otp_code(self, reference):
            assert reference == ""
            otp_events.append("lookup")
            return "987654"

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(cli, "CommandSecretProvider", FakeProvider)
    return otp_events


def test_cli_reads_credentials_from_external_secret_provider(tmp_path, monkeypatch):
    config_path = _write_external_provider_config(tmp_path)
    otp_calls = _install_external_provider(monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    ctx = cli.context_from_args(args, parser)
    config = ctx.config

    assert config.login_provider == "1password"
    assert config.secret_password_ref == "op://Private/RWTH/password"
    assert config.secret_otp_ref == "op://Private/RWTH/otp?attribute=otp"
    assert ctx.auth.otp_code is None
    assert otp_calls == []
    assert ctx.auth.credential_resolver is not None
    ctx.auth.credential_resolver()
    assert ctx.auth.password == "provider-password"
    assert ctx.auth.otp_code_resolver is not None
    assert ctx.auth.otp_code_resolver() == "123456"
    assert otp_calls == ["op://Private/RWTH/otp?attribute=otp"]


def test_auth_login_totp_manual_skips_external_provider_otp(tmp_path, monkeypatch):
    config_path = _write_external_provider_config(tmp_path)
    otp_calls = _install_external_provider(monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--config", str(config_path), "auth", "login", "--totp-manual"]
    )

    ctx = cli.configured_auth_context(args, parser, None)

    assert ctx.auth.credential_resolver is not None
    ctx.auth.credential_resolver()
    assert ctx.auth.password == "provider-password"
    assert ctx.auth.otp_code is None
    assert ctx.auth.otp_code_resolver is None
    assert otp_calls == []


def test_external_secret_provider_requires_password_ref(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[auth]
user = "user"

[auth.login]
provider = "pass"
totp_serial = "totp"
""",
        encoding="utf-8",
    )

    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.context_from_args(args, parser)

    assert exc_info.value.code == 2
    assert "auth.login.password is required" in capsys.readouterr().err


def test_external_secret_provider_availability_errors_fail_clearly(
    tmp_path, monkeypatch, caplog
):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[auth]
user = "user"

[auth.login]
provider = "bitwarden"
totp_serial = "totp"
password = "rwth"
""",
        encoding="utf-8",
    )

    class FakeProvider:
        def check_available(self):
            raise cli.ProviderSecretError("bw status failed")

        def get_password(self, reference):
            pytest.fail(f"unexpected password lookup: {reference}")

    monkeypatch.setattr(
        cli,
        "build_external_secret_provider",
        lambda provider_name: FakeProvider(),
    )
    caplog.set_level(logging.CRITICAL, logger="syncmymoodle.cli")
    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    ctx = cli.context_from_args(args, parser)

    assert ctx.auth.credential_resolver is not None
    with pytest.raises(SystemExit) as exc_info:
        ctx.auth.credential_resolver()
    assert exc_info.value.code == 1
    assert "bw status failed" in caplog.text


def test_command_secret_provider_requires_argv_arrays():
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(
            {
                "auth": {
                    "login": {
                        "provider": "command",
                        "password_command": "secret-tool lookup rwth",
                    }
                }
            }
        )

    assert "auth.login.password_command must be an array" in str(exc_info.value)


def test_command_secret_provider_rejects_explicit_config(tmp_path, caplog):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[auth]
user = "user"

[auth.login]
provider = "command"
totp_serial = "totp"
password_command = ["secret-tool", "lookup", "rwth"]
""",
        encoding="utf-8",
    )
    caplog.set_level(logging.CRITICAL, logger="syncmymoodle.cli")
    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.context_from_args(args, parser)

    assert exc_info.value.code == 1
    assert "only allowed from the default global config" in caplog.text


def test_command_secret_provider_reads_from_global_config(
    tmp_path,
    monkeypatch,
):
    otp_events = _install_command_provider(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args([])

    ctx = cli.context_from_args(args, parser)

    assert ctx.auth.password is None
    assert ctx.auth.otp_code is None
    assert ctx.auth.credential_resolver is not None
    ctx.auth.credential_resolver()
    assert ctx.auth.password == "command-password"
    assert ctx.auth.otp_code_resolver is not None
    assert ctx.auth.otp_code_resolver() == "987654"
    assert otp_events == ["availability", "lookup"]


def test_auth_login_totp_manual_skips_command_provider_otp(tmp_path, monkeypatch):
    otp_events = _install_command_provider(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["auth", "login", "--totp-manual"])

    ctx = cli.configured_auth_context(args, parser, None)

    assert ctx.auth.credential_resolver is not None
    ctx.auth.credential_resolver()
    assert ctx.auth.password == "command-password"
    assert ctx.auth.otp_code is None
    assert ctx.auth.otp_code_resolver is None
    assert otp_events == []


def test_env_file_without_password_fails_clearly(tmp_path, caplog):
    env_path = tmp_path / "secrets.env"
    env_path.write_text("SYNCMYMOODLE_TOTP_SECRET=totp-secret\n", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[auth]
user = "user"

[auth.login]
provider = "env-file"
totp_serial = "totp"
env_file = {str(env_path)!r}
""",
        encoding="utf-8",
    )
    caplog.set_level(logging.CRITICAL, logger="syncmymoodle.cli")
    parser = cli.build_parser()
    args = parser.parse_args(["--config", str(config_path)])

    ctx = cli.context_from_args(args, parser)

    assert ctx.auth.credential_resolver is not None
    with pytest.raises(SystemExit) as exc_info:
        ctx.auth.credential_resolver()
    assert exc_info.value.code == 1
    assert "auth.login.env_file does not define SYNCMYMOODLE_PASSWORD" in caplog.text


def test_cli_overrides_are_applied_after_config(tmp_path):
    login_env_file = tmp_path / "smm-secrets.env"
    cookie_file = tmp_path / "session"
    sync_directory = tmp_path / "moodle"
    browser = tmp_path / "chromium"
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--user",
            "cli-user",
            "--totp-serial",
            "cli-totp",
            "--login-env-file",
            str(login_env_file),
            "--cookie-file",
            str(cookie_file),
            "--courses",
            "course-a,course-b",
            "--skip-courses",
            "course-c,course-d",
            "--semesters",
            "25ws,26ss",
            "--sync-directory",
            str(sync_directory),
            "--browser",
            str(browser),
            "--course-prefix-handling",
            "suffix",
            "--no-follow-links",
            "--no-youtube",
            "--no-opencast",
            "--no-sciebo",
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
                "login": {"totp_serial": "config-totp"},
            },
            "links": {"opencast": True},
            "modules": {"folder": False, "quiz": "off"},
        }
    )

    cli.apply_cli_overrides(config, args)
    cfg = Config.from_dict(config)

    assert cfg.user == "cli-user"
    assert cfg.totp_serial == "cli-totp"
    assert cfg.login_env_file == str(login_env_file)
    assert cfg.cookie_file == str(cookie_file)
    assert cfg.selected_courses == ["course-a", "course-b"]
    assert cfg.skip_courses == ["course-c", "course-d"]
    assert cfg.only_sync_semester == ["25ws", "26ss"]
    assert cfg.sync_directory == str(sync_directory)
    assert cfg.browser == str(browser)
    assert cfg.course_prefix_handling == "suffix"
    assert cfg.keyring_store_totp_secret is False
    assert cfg.follow_links is False  # --no-follow-links
    assert cfg.link_youtube is False
    assert cfg.link_opencast is False
    assert cfg.link_sciebo is False
    assert cfg.exclude_filetypes == ["pdf", "mp4"]
    assert cfg.exclude_files == ["*.bak", "*.tmp"]
    assert cfg.exclude_links == {"*": ["*calendar*", "*hinge*"]}
    assert cfg.allowed_domains == {
        "*": ["moodle.rwth-aachen.de", "rwth-aachen.sciebo.de"]
    }
    assert cfg.exclude_sections == {"*": ["General", "Week 1"]}
    assert cfg.exclude_modules == {"*": ["Quiz*", "resource"]}
    assert cfg.quiz_mode == "pdf"  # CLI beats the config's "off"
    assert cfg.module_folder is False
    assert cfg.update_files is True
    assert cfg.conflict_handling == "keep"


def test_boolean_cli_options_override_config_in_both_directions():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--no-update-files", "--no-dry-run", "--follow-links", "--opencast"]
    )
    config = canonicalize(
        {
            "downloads": {"update_files": True, "dry_run": True},
            "links": {"follow_links": False, "opencast": False},
        }
    )

    cli.apply_cli_overrides(config, args)
    resolved = Config.from_dict(config)

    assert resolved.update_files is False
    assert resolved.dry_run is False
    assert resolved.follow_links is True
    assert resolved.link_opencast is True


def test_empty_csv_cli_options_clear_configured_lists():
    parser = cli.build_parser()
    args = parser.parse_args(["--courses", "", "--allowed-domains", ""])
    config = canonicalize(
        {
            "courses": {"selected": ["123"]},
            "filters": {"allowed_domains": ["moodle.rwth-aachen.de"]},
        }
    )

    cli.apply_cli_overrides(config, args)
    resolved = Config.from_dict(config)

    assert resolved.selected_courses == []
    assert resolved.allowed_domains == {}


def test_login_env_file_cli_override_selects_env_file_provider(tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args(["--login-env-file", "login.env"])
    config = canonicalize(
        {
            "auth": {
                "login": {
                    "provider": "1password",
                    "password": "op://Private/RWTH/password",
                    "otp": "op://Private/RWTH/otp",
                }
            }
        }
    )

    cli.apply_cli_overrides(config, args, path_base=tmp_path)
    resolved = Config.from_dict(config)

    assert resolved.login_provider == "env-file"
    assert resolved.login_env_file == str(tmp_path / "login.env")
    assert resolved.secret_password_ref is None
    assert resolved.secret_otp_ref is None


def test_quiz_cli_override_keeps_other_modules_enabled():
    # Regression test: --quiz must not disable every other module when the
    # config has no module settings at all.
    parser = cli.build_parser()
    args = parser.parse_args(["--quiz", "pdf"])
    config: dict = {}

    cli.apply_cli_overrides(config, args)
    cfg = Config.from_dict(config)

    assert cfg.quiz_mode == "pdf"
    assert cfg.module_assignment
    assert cfg.module_resource
    assert cfg.module_folder
    assert cfg.link_source_enabled("opencast")


def test_cli_keyring_resolution_reads_password_and_totp_secret():
    calls = []
    fake_keyring = SimpleNamespace(
        get_keyring=lambda: object(),
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
                "login": {
                    "provider": "keyring",
                    "totp_serial": "totp-provider",
                    "keyring_store_totp_secret": True,
                },
            }
        }
    )

    auth = AuthState.from_config(config)
    cli.resolve_keyring_credentials(auth, True, fake_keyring)

    assert auth.password == "stored-password"
    assert auth.totp_secret == "stored-totp-secret"
    assert calls == [
        ("syncmymoodle", "user"),
        ("syncmymoodle", "totp-provider"),
    ]


def test_auth_login_totp_manual_skips_keyring_totp_secret(tmp_path):
    calls = []
    fake_keyring = SimpleNamespace(
        get_keyring=lambda: object(),
        get_password=lambda service, name: (
            calls.append((service, name)) or {"user": "stored-password"}[name]
        ),
        set_password=lambda service, name, value: calls.append((service, name, value)),
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[auth]
user = "user"

[auth.login]
provider = "keyring"
totp_serial = "totp-provider"
keyring_store_totp_secret = true
""",
        encoding="utf-8",
    )
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--config", str(config_path), "auth", "login", "--totp-manual"]
    )

    ctx = cli.configured_auth_context(args, parser, fake_keyring)
    assert ctx.auth.credential_resolver is not None
    ctx.auth.credential_resolver()

    assert ctx.auth.password == "stored-password"
    assert ctx.auth.totp_secret is None
    assert calls == [("syncmymoodle", "user")]


def test_preloaded_password_avoids_keyring_access():
    stored: dict = {}
    fake_keyring = FakeKeyring(stored)
    config = Config.from_dict(
        {
            "auth": {
                "user": "user",
                "login": {"provider": "keyring", "totp_serial": "totp"},
            }
        }
    )
    auth = AuthState.from_config(config)
    auth.password = "preloaded-password"

    cli.configure_keyring_resolver(
        auth,
        config.auth_source,
        fake_keyring,
        resolve_otp=True,
    )

    assert stored == {}
    assert auth.password == "preloaded-password"
    assert auth.credential_resolver is None


def test_empty_keyring_password_reprompts_before_storing(monkeypatch):
    stored: dict = {("syncmymoodle", "user"): ""}
    fake_keyring = FakeKeyring(stored)
    config = Config.from_dict(
        {
            "auth": {
                "user": "user",
                "login": {"provider": "keyring", "totp_serial": "totp"},
            }
        }
    )
    monkeypatch.setattr(
        "syncmymoodle.output.getpass.getpass", lambda prompt: "prompt-password"
    )

    auth = AuthState.from_config(config)
    cli.resolve_keyring_credentials(auth, False, fake_keyring)

    assert auth.password == "prompt-password"
    assert stored[("syncmymoodle", "user")] == "prompt-password"


def test_empty_prompted_keyring_password_fails_without_storing(monkeypatch, caplog):
    stored: dict = {}
    fake_keyring = FakeKeyring(stored)
    config = Config.from_dict(
        {
            "auth": {
                "user": "user",
                "login": {"provider": "keyring", "totp_serial": "totp"},
            }
        }
    )
    monkeypatch.setattr("syncmymoodle.output.getpass.getpass", lambda prompt: "")
    caplog.set_level(logging.CRITICAL, logger="syncmymoodle.cli")

    with pytest.raises(SystemExit) as exc_info:
        cli.resolve_keyring_credentials(
            AuthState.from_config(config),
            False,
            fake_keyring,
        )

    assert exc_info.value.code == 1
    assert stored == {}
    assert "Password is required" in caplog.text


def test_explicit_legacy_json_requires_migration(tmp_path, capsys):
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

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--config", str(config_path)])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "could not parse config file" in error
    assert f"syncmymoodle config migrate --input {config_path}" in error
