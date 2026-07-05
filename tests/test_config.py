import json
import sys

import syncmymoodle.cli as cli
from syncmymoodle.config import Config


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
