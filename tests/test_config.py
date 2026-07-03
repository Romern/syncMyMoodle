import json
import sys
from types import SimpleNamespace

import syncmymoodle.cli as cli
from syncmymoodle.app import SyncMyMoodle
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
    assert cfg.exclude_links == []
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
    assert cfg.exclude_sections == ["Hidden*"]
    assert cfg.exclude_modules == ["Skip Module"]


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


def test_quiz_is_forced_off_even_when_enabled():
    cfg = Config.from_dict({"used_modules": {"url": {"quiz": True, "opencast": True}}})
    assert cfg.url_module_enabled("quiz") is False
    assert cfg.url_module_enabled("opencast") is True


def test_from_dict_does_not_mutate_input():
    raw = {"used_modules": {"url": {"quiz": True, "opencast": True}}}
    cfg = Config.from_dict(raw)
    assert cfg.url_module_enabled("quiz") is False
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


def test_syncmymoodle_initializes_context_config_from_dict():
    smm = SyncMyMoodle({"basedir": "/tmp/syncmymoodle-test"})
    assert smm.ctx.config.basedir == "/tmp/syncmymoodle-test"


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
    captured_config = {}

    class FakeSyncMyMoodle:
        def __init__(self, config):
            captured_config.update(config)
            self.ctx = SimpleNamespace(opencast_error_count=0)

        def login(self):
            pass

        def get_moodle_wstoken(self):
            pass

        def get_userid(self):
            pass

        def sync(self):
            pass

        def download_all_files(self):
            pass

        def cache_root_node(self):
            pass

    monkeypatch.setattr(cli, "SyncMyMoodle", FakeSyncMyMoodle)
    monkeypatch.setattr(
        sys,
        "argv",
        ["syncmymoodle", "--config", str(config_path)],
    )

    cli.main()

    assert captured_config["nolinks"] is True
    assert captured_config["updatefiles"] is True
