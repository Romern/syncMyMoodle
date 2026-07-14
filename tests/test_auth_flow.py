from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import syncmymoodle.cli as cli
from syncmymoodle.config import Config
from syncmymoodle.context import BrowserSessionUnavailable, MoodleAccount, SyncContext
from syncmymoodle.moodle_tokens import (
    EnvFileTokenStore,
    KeyringTokenStore,
    MoodleTokens,
)
from syncmymoodle.secret_providers import KeyringProvider, ProviderAvailability

from .helpers import FakeKeyring


class MemoryStore:
    description = "memory store"

    def __init__(self, tokens: MoodleTokens | None) -> None:
        self.tokens = tokens
        self.writes: list[MoodleTokens] = []

    def check_available(self) -> ProviderAvailability:
        return ProviderAvailability(True)

    def load(self) -> MoodleTokens | None:
        return self.tokens

    def store(self, tokens: MoodleTokens) -> None:
        self.tokens = tokens
        self.writes.append(tokens)

    def delete(self) -> None:
        self.tokens = None


def tokens(username: str = "ab123456") -> MoodleTokens:
    return MoodleTokens(
        username,
        "ws-token",
        "private-token",
        moodle_user_id=123,
    )


def valid(tokens: MoodleTokens) -> cli.moodle_api.TokenValidation:
    return cli.moodle_api.TokenValidation(
        cli.moodle_api.TokenValidationKind.VALID,
        site_info={
            "userid": tokens.moodle_user_id,
            "username": tokens.username,
            "siteurl": "https://moodle.rwth-aachen.de/",
            "userprivateaccesskey": "download-key",
        },
    )


def write_env_token_config(tmp_path: Path) -> tuple[Path, Path, MoodleTokens]:
    config_path = tmp_path / "config.toml"
    token_path = tmp_path / "mobile-token.env"
    stored = tokens()
    EnvFileTokenStore(token_path, stored.username).store(stored)
    config_path.write_text(
        f"""
[auth]
user = "{stored.username}"

[auth.tokens]
store = "env-file"
env_file = {str(token_path)!r}

[auth.login]
provider = "prompt"

[paths]
cookie_file = {str(tmp_path / "session")!r}
""",
        encoding="utf-8",
    )
    return config_path, token_path, stored


def test_setup_logs_in_once_and_writes_only_non_secret_config(
    tmp_path, monkeypatch, capsys
):
    config_home = tmp_path / "xdg"
    fake_keyring = FakeKeyring()
    stored = tokens()
    answers = iter([stored.username, "TOTP123", str(tmp_path / "sync"), ""])
    login_calls = []

    def answer():
        return next(answers)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(cli, "load_keyring_backend", lambda: fake_keyring)
    monkeypatch.setattr(cli, "detect_password_manager_clis", lambda: ())
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(
        cli.rwth,
        "login",
        lambda ctx, log, *, reuse_cached_session, persist_session: login_calls.append(
            (reuse_cached_session, persist_session)
        ),
    )
    monkeypatch.setattr(
        cli, "acquire_validated_moodle_tokens", lambda ctx, parser: stored
    )
    monkeypatch.setattr(
        cli.moodle_api,
        "reset_mobile_token",
        lambda *args: pytest.fail("setup must not reset the shared token"),
    )

    cli.main(["setup"])
    output = capsys.readouterr().out

    config_path = config_home / "syncmymoodle" / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    expected = tomllib.loads(cli.starter_config_text())
    expected["auth"]["user"] = stored.username
    expected["auth"]["login"]["totp_serial"] = "TOTP123"
    expected["paths"]["sync_directory"] = str(tmp_path / "sync")
    assert parsed == expected
    assert "# Relative paths in this file resolve" in text
    assert "ws-token" not in text
    assert "private-token" not in text
    assert login_calls == [(False, False)]
    assert len(fake_keyring.values) == 1
    assert MoodleTokens.from_json(next(iter(fake_keyring.values.values()))) == stored
    assert "RWTH SSO TOTP serial (for example, TOTP12345678): " in output
    assert "Directory to sync Moodle files to [" in output
    assert "Store Moodle tokens in the system keyring (recommended) [Y/n]: " in output
    assert "Normal syncs use the stored Moodle tokens" in output


def test_setup_rolls_back_tokens_when_config_write_fails(tmp_path, monkeypatch):
    original = tokens()
    replacement = MoodleTokens(
        original.username,
        "replacement-ws-token",
        "replacement-private-token",
        moodle_user_id=original.moodle_user_id,
    )
    store = MemoryStore(original)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(
        cli,
        "prompt_setup_config",
        lambda parser: (
            {
                "auth.user": original.username,
                "auth.login.totp_serial": "TOTP123",
                "auth.login.provider": "prompt",
                "paths.sync_directory": str(tmp_path / "sync"),
            },
            original.username,
            "TOTP123",
        ),
    )
    monkeypatch.setattr(cli, "prompt_setup_password_manager", lambda *args: None)
    monkeypatch.setattr(cli, "prompt_setup_token_store", lambda *args: None)
    monkeypatch.setattr(cli, "token_store_from_config", lambda *args: store)

    def login(ctx, log, *, reuse_cached_session, persist_session):
        assert reuse_cached_session is False
        assert persist_session is False

    monkeypatch.setattr(cli.rwth, "login", login)
    monkeypatch.setattr(
        cli,
        "acquire_validated_moodle_tokens",
        lambda ctx, parser: replacement,
    )
    monkeypatch.setattr(
        cli,
        "write_private_text",
        lambda *args: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["setup"])

    assert exc_info.value.code == 2
    assert store.tokens == original
    assert store.writes == [replacement, original]
    assert not (tmp_path / "xdg" / "syncmymoodle" / "config.toml").exists()


def test_setup_token_file_fallback_prompt_is_clear(tmp_path, monkeypatch, capsys):
    token_path = tmp_path / "tokens.env"
    config = {}

    def answer():
        return str(token_path)

    monkeypatch.setattr(cli, "load_keyring_backend", lambda: None)
    monkeypatch.setattr("builtins.input", answer)

    cli.prompt_setup_token_store(config, "ab123456", None)

    assert (
        "File for securely storing Moodle tokens "
        f"[{cli.pathing.user_config_dir() / 'moodle-tokens.env'}]: "
        in capsys.readouterr().out
    )
    assert config == {
        "auth.tokens.store": "env-file",
        "auth.tokens.env_file": str(token_path),
    }


def test_setup_configures_detected_password_manager_and_uses_it_for_login(
    tmp_path, monkeypatch, capsys
):
    config_home = tmp_path / "xdg"
    fake_keyring = FakeKeyring()
    stored = tokens()
    password_ref = "op://Private/RWTH/password"
    otp_ref = "op://Private/RWTH/one-time password?attribute=otp"
    built_providers = []
    answers = iter(
        [
            stored.username,
            "TOTP123",
            str(tmp_path / "sync"),
            "y",
            f'"{password_ref}"',
            f'"{otp_ref}"',
            "",
        ]
    )

    class FakeProvider:
        def check_available(self):
            return ProviderAvailability(True)

        def get_password(self, reference):
            assert reference == password_ref
            return "provider-password"

        def get_otp_code(self, reference):
            assert reference == otp_ref
            return "123456"

    def login(ctx, log, *, reuse_cached_session, persist_session):
        assert reuse_cached_session is False
        assert persist_session is False
        assert ctx.auth.credential_resolver is not None
        ctx.auth.credential_resolver()
        assert ctx.auth.password == "provider-password"
        assert ctx.auth.otp_code_resolver is not None
        assert ctx.auth.otp_code_resolver() == "123456"

    def build_provider(provider_name):
        built_providers.append(provider_name)
        return FakeProvider()

    def answer():
        return next(answers)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(cli, "load_keyring_backend", lambda: fake_keyring)
    monkeypatch.setattr(
        cli,
        "detect_password_manager_clis",
        lambda: ("1password", "bitwarden"),
    )
    monkeypatch.setattr(
        cli,
        "build_external_secret_provider",
        build_provider,
    )
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(cli.rwth, "login", login)
    monkeypatch.setattr(
        cli, "acquire_validated_moodle_tokens", lambda ctx, parser: stored
    )

    cli.main(["setup"])
    output = capsys.readouterr().out

    parsed = tomllib.loads(
        (config_home / "syncmymoodle" / "config.toml").read_text(encoding="utf-8")
    )
    login_config = parsed["auth"]["login"]
    assert login_config["totp_serial"] == "TOTP123"
    assert login_config["provider"] == "1password"
    assert login_config["password"] == password_ref
    assert login_config["otp"] == otp_ref
    assert login_config["password_command"] == []
    assert built_providers == ["1password"]
    assert (
        "1Password password reference "
        "(for example, op://Private/RWTH/password)" in output
    )
    assert (
        "1Password TOTP reference "
        "(for example, op://Private/RWTH/one-time password?attribute=otp" in output
    )
    assert "automatically" not in output


def test_setup_explains_limited_opencast_before_recommending_token_reset(
    tmp_path, monkeypatch, capsys
):
    config_home = tmp_path / "xdg"
    fake_keyring = FakeKeyring()
    legacy_tokens = MoodleTokens(
        "ab123456",
        "legacy-ws-token",
        None,
        moodle_user_id=123,
    )
    answers = iter(
        [
            legacy_tokens.username,
            "TOTP123",
            str(tmp_path / "sync"),
            "",
            "y",
        ]
    )

    def answer():
        return next(answers)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(cli, "load_keyring_backend", lambda: fake_keyring)
    monkeypatch.setattr(cli, "detect_password_manager_clis", lambda: ())
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(cli.rwth, "login", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "acquire_validated_moodle_tokens",
        lambda ctx, parser: legacy_tokens,
    )

    cli.main(["setup"])

    assert (config_home / "syncmymoodle" / "config.toml").is_file()
    stored = MoodleTokens.from_json(next(iter(fake_keyring.values.values())))
    assert stored == legacy_tokens
    captured = capsys.readouterr()
    assert (
        "browser login token required for embedded Opencast downloads" in captured.out
    )
    assert "limited Opencast support" in captured.out
    assert "Setup complete" in captured.out
    assert "auth reset-token" in captured.err
    assert "cached browser session expires" in captured.err


def test_setup_points_legacy_json_users_to_migration(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "xdg" / "syncmymoodle"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.json"
    config_path.write_text('{"user": "ab123456"}', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(
        "builtins.input", lambda: pytest.fail("setup must stop before prompting")
    )

    with pytest.raises(SystemExit) as error:
        cli.main(["setup"])

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert str(config_path) in stderr
    assert "syncmymoodle config migrate" in stderr


def test_setup_points_existing_toml_users_to_manual_editing(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "xdg" / "syncmymoodle" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('[auth]\nuser = "ab123456"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(
        "builtins.input", lambda: pytest.fail("setup must stop before prompting")
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["setup"])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert str(config_path) in error
    assert "edit that file manually" in error
    assert "syncmymoodle config path" in error
    assert "syncmymoodle auth status" in error


def test_auth_status_is_read_only_and_reports_exact_cached_session_time(
    tmp_path, monkeypatch, capsys
):
    config_path, _, stored = write_env_token_config(tmp_path)
    monkeypatch.setattr(
        cli.moodle_api,
        "validate_mobile_tokens",
        lambda value: valid(value),
    )
    monkeypatch.setattr(
        cli.rwth,
        "cached_session_status",
        lambda path: cli.rwth.SessionStatus(
            cli.rwth.SessionStatusKind.VALID, remaining_seconds=50397
        ),
    )
    monkeypatch.setattr(
        cli.moodle_api,
        "create_browser_session",
        lambda *args: pytest.fail("status must not consume an auto-login key"),
    )
    monkeypatch.setattr(
        cli.rwth, "login", lambda *args: pytest.fail("status must not perform SSO")
    )

    cli.main(["--config", str(config_path), "auth", "status"])

    output = capsys.readouterr().out
    assert "RWTH sign-in method: interactive prompt when needed" in output
    assert "Moodle API token: valid" in output
    assert "API token expiry: not reported by Moodle" in output
    assert "Browser login token: present" in output
    assert "not tested because Moodle limits how often it can be used" in output
    assert "Cached browser session: valid" in output
    assert "Remaining: 13h 59m 57s" in output
    assert stored.wstoken not in output


def test_auth_status_colors_invalid_and_expired_states_on_stdout(
    tmp_path, monkeypatch, capsys
):
    config_path, _, _ = write_env_token_config(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(
        cli.moodle_api,
        "validate_mobile_tokens",
        lambda value: cli.moodle_api.TokenValidation(
            cli.moodle_api.TokenValidationKind.INVALID, "Moodle rejected it"
        ),
    )
    monkeypatch.setattr(
        cli.rwth,
        "cached_session_status",
        lambda path: cli.rwth.SessionStatus(cli.rwth.SessionStatusKind.EXPIRED),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "--config",
                str(config_path),
                "--color",
                "always",
                "auth",
                "status",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert (
        "\x1b[31mMoodle API token: invalid (Moodle rejected it)\x1b[0m" in captured.out
    )
    assert "\x1b[33mCached browser session: expired\x1b[0m" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("availability", "expected_state"),
    [
        (ProviderAvailability(True), "available"),
        (
            ProviderAvailability(False, "'op' executable not found"),
            "unavailable: 'op' executable not found",
        ),
    ],
)
def test_sign_in_method_checks_password_manager_availability(
    monkeypatch, availability, expected_state
):
    config = Config.from_dict(
        {
            "auth": {
                "login": {
                    "provider": "1password",
                    "password": "op://Private/RWTH/password",
                }
            }
        }
    )
    provider = SimpleNamespace(check_available=lambda: availability)
    monkeypatch.setattr(
        cli, "build_external_secret_provider", lambda provider_name: provider
    )

    description, available = cli.sign_in_method_status(config, None)

    assert description == f"1Password CLI ({expected_state})"
    assert available is availability.available


def test_sign_in_method_checks_configured_otp_command(monkeypatch):
    config = Config.from_dict(
        {
            "auth": {
                "login": {
                    "provider": "command",
                    "password_command": ["password-helper"],
                    "otp_command": ["otp-helper"],
                }
            }
        }
    )
    provider = SimpleNamespace(
        check_available=lambda: ProviderAvailability(True),
        check_otp_available=lambda: ProviderAvailability(
            False, "'otp-helper' executable not found"
        ),
    )
    monkeypatch.setattr(cli, "CommandSecretProvider", lambda *args: provider)

    description, available = cli.sign_in_method_status(config, None)

    assert "unavailable: 'otp-helper' executable not found" in description
    assert available is False


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("login", "does not revoke the shared Moodle API token"),
        ("migrate", "previous store is left untouched"),
        ("status", "without signing in"),
        ("forget", "RWTH sign-in secrets remain"),
        ("reset-token", "every other syncMyMoodle installation"),
    ],
)
def test_auth_leaf_help_explains_effects(command, expected, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["auth", command, "--help"])

    assert exc_info.value.code == 0
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize(
    "auth_args",
    [
        ["auth", "status"],
        ["auth", "migrate", "--to", "keyring"],
        ["auth", "forget"],
        ["auth", "reset-token"],
    ],
)
def test_auth_commands_report_malformed_config_cleanly(auth_args, tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[auth\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--config", str(config_path), *auth_args])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert f"could not parse config file {config_path}" in error
    assert "Traceback" not in error


def test_auth_login_replaces_local_pair_without_resetting_shared_token(
    tmp_path, monkeypatch
):
    config_path, token_path, _ = write_env_token_config(tmp_path)
    replacement = MoodleTokens(
        "ab123456",
        "replacement-ws",
        "replacement-private",
        moodle_user_id=123,
    )
    login_calls = []
    monkeypatch.setattr(
        cli.rwth,
        "login",
        lambda ctx, log, *, reuse_cached_session: login_calls.append(
            reuse_cached_session
        ),
    )
    monkeypatch.setattr(
        cli, "acquire_validated_moodle_tokens", lambda ctx, parser: replacement
    )
    monkeypatch.setattr(
        cli.moodle_api,
        "reset_mobile_token",
        lambda *args: pytest.fail("auth login must not reset the shared token"),
    )

    cli.main(["--config", str(config_path), "auth", "login"])

    assert login_calls == [False]
    assert EnvFileTokenStore(token_path, replacement.username).load() == (replacement)


def test_auth_migrate_to_keyring_validates_destination_and_leaves_source(
    tmp_path, monkeypatch, capsys
):
    config_path, token_path, stored = write_env_token_config(tmp_path)
    keyring = FakeKeyring()
    monkeypatch.setattr(cli, "load_keyring_backend", lambda: keyring)
    monkeypatch.setattr(
        cli.moodle_api,
        "validate_mobile_tokens",
        lambda value: valid(value),
    )

    cli.main(["--config", str(config_path), "auth", "migrate", "--to", "keyring"])

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["auth"]["tokens"] == {"store": "keyring"}
    assert token_path.is_file()
    assert MoodleTokens.from_json(next(iter(keyring.values.values()))) == stored
    output = capsys.readouterr().out
    assert "Copied Moodle tokens to system keyring and updated" in output
    assert "left untouched" in output
    assert "Moved Moodle tokens" not in output


def test_auth_migrate_can_login_when_source_store_has_no_token(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    missing_token_path = tmp_path / "missing-token.env"
    destination_path = tmp_path / "destination-token.env"
    stored = tokens()
    config_path.write_text(
        f"""
[auth]
user = "{stored.username}"

[auth.tokens]
store = "env-file"
env_file = {str(missing_token_path)!r}

[auth.login]
provider = "prompt"
""",
        encoding="utf-8",
    )
    login_calls = []
    monkeypatch.setattr(
        cli.rwth,
        "login",
        lambda ctx, log, *, reuse_cached_session: login_calls.append(
            reuse_cached_session
        ),
    )
    monkeypatch.setattr(
        cli, "acquire_validated_moodle_tokens", lambda ctx, parser: stored
    )

    cli.main(
        [
            "--config",
            str(config_path),
            "auth",
            "migrate",
            "--to",
            "env-file",
            "--env-file",
            str(destination_path),
        ]
    )

    assert login_calls == [False]
    assert not missing_token_path.exists()
    assert EnvFileTokenStore(destination_path, stored.username).load() == (stored)


def test_auth_migrate_restores_destination_if_config_write_fails(tmp_path, monkeypatch):
    config_path, _, source_tokens = write_env_token_config(tmp_path)
    original_config = config_path.read_text(encoding="utf-8")
    destination_path = tmp_path / "destination-token.env"
    destination_tokens = MoodleTokens(
        source_tokens.username,
        "previous-ws-token",
        "previous-private-token",
        moodle_user_id=source_tokens.moodle_user_id,
    )
    destination = EnvFileTokenStore(destination_path, source_tokens.username)
    destination.store(destination_tokens)
    monkeypatch.setattr(
        cli.moodle_api,
        "validate_mobile_tokens",
        lambda value: valid(value),
    )

    def fail_config_write(*args):
        raise PermissionError("read-only config")

    monkeypatch.setattr(cli, "write_private_text", fail_config_write)

    with pytest.raises(SystemExit):
        cli.main(
            [
                "--config",
                str(config_path),
                "auth",
                "migrate",
                "--to",
                "env-file",
                "--env-file",
                str(destination_path),
            ]
        )

    assert config_path.read_text(encoding="utf-8") == original_config
    assert destination.load() == destination_tokens


def test_auth_migrate_rejects_destination_aliasing_config_before_write(
    tmp_path, monkeypatch, capsys
):
    config_path, _, stored = write_env_token_config(tmp_path)
    original_config = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        cli.moodle_api,
        "validate_mobile_tokens",
        lambda value: valid(value),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "--config",
                str(config_path),
                "auth",
                "migrate",
                "--to",
                "env-file",
                "--env-file",
                str(config_path),
            ]
        )

    assert exc_info.value.code == 2
    assert "configuration file" in capsys.readouterr().err
    assert config_path.read_text(encoding="utf-8") == original_config
    assert stored.wstoken not in original_config


def test_auth_reset_token_requires_confirmation(tmp_path, monkeypatch, capsys):
    config_path, _, _ = write_env_token_config(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("builtins.input", lambda: "n")
    monkeypatch.setattr(
        cli.rwth, "login", lambda *args: pytest.fail("cancelled reset performed SSO")
    )
    monkeypatch.setattr(
        cli.moodle_api,
        "reset_mobile_token",
        lambda *args: pytest.fail("cancelled reset changed server state"),
    )

    cli.main(
        [
            "--config",
            str(config_path),
            "--color",
            "always",
            "auth",
            "reset-token",
        ]
    )

    output = capsys.readouterr().out
    assert "\x1b[33mThis resets the shared Moodle API token.\x1b[0m" in output
    assert "every other syncMyMoodle installation" in output
    assert "Other Moodle service tokens are unaffected" in output
    assert "Token reset cancelled" in output


def test_auth_forget_removes_only_local_env_token_and_session(
    tmp_path, monkeypatch, capsys
):
    config_path, token_path, _ = write_env_token_config(tmp_path)
    cookie_path = tmp_path / "session"
    cookie_path.write_bytes(b"cached session")
    original_config = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda: "y")
    monkeypatch.setattr(
        cli.rwth, "login", lambda *args: pytest.fail("forget must not perform SSO")
    )
    monkeypatch.setattr(
        cli.moodle_api,
        "reset_mobile_token",
        lambda *args: pytest.fail("forget must not reset the shared token"),
    )

    cli.main(["--config", str(config_path), "auth", "forget"])

    assert not token_path.exists()
    assert not cookie_path.exists()
    assert config_path.read_text(encoding="utf-8") == original_config
    output = capsys.readouterr().out
    assert "RWTH sign-in secrets will remain unchanged" in output
    assert "shared Moodle API token was not reset" in output


def test_auth_forget_requires_confirmation(tmp_path, monkeypatch, capsys):
    config_path, token_path, _ = write_env_token_config(tmp_path)
    cookie_path = tmp_path / "session"
    cookie_path.write_bytes(b"cached session")
    monkeypatch.delenv("NO_COLOR", raising=False)

    def decline():
        return "n"

    monkeypatch.setattr("builtins.input", decline)

    cli.main(
        [
            "--config",
            str(config_path),
            "--color",
            "always",
            "auth",
            "forget",
        ]
    )

    assert token_path.is_file()
    assert cookie_path.is_file()
    output = capsys.readouterr().out
    assert (
        "\x1b[33mThis removes authentication data stored only on this installation."
        "\x1b[0m" in output
    )
    assert (
        "\x1b[33mForget local Moodle tokens and cached browser session [y/N]: "
        "\x1b[0m" in output
    )
    assert "left unchanged" in output


def test_auth_forget_deletes_keyring_record(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    stored = tokens()
    fake_keyring = FakeKeyring()
    store = KeyringTokenStore(KeyringProvider(fake_keyring), stored.username)
    store.store(stored)
    config_path.write_text(
        f"""
[auth]
user = "{stored.username}"

[auth.tokens]
store = "keyring"

[auth.login]
provider = "prompt"

[paths]
cookie_file = {str(tmp_path / "session")!r}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "load_keyring_backend", lambda: fake_keyring)
    monkeypatch.setattr("builtins.input", lambda: "y")

    cli.main(["--config", str(config_path), "auth", "forget"])

    assert fake_keyring.values == {}


def test_auth_forget_still_removes_session_when_token_delete_fails(
    tmp_path, monkeypatch, capsys
):
    config_path, _, stored = write_env_token_config(tmp_path)
    cookie_path = tmp_path / "session"
    cookie_path.write_bytes(b"cached session")
    store = MemoryStore(stored)

    def fail_delete() -> None:
        raise cli.ProviderSecretError("keyring is locked")

    store.delete = fail_delete  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "token_store_from_config", lambda config, backend: store)
    monkeypatch.setattr("builtins.input", lambda: "y")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--config", str(config_path), "auth", "forget"])

    assert exc_info.value.code == 2
    assert not cookie_path.exists()
    assert "keyring is locked" in capsys.readouterr().err


def test_normal_sync_uses_valid_stored_token_without_sso(monkeypatch):
    stored = tokens()
    store = MemoryStore(stored)
    ctx = SyncContext(Config.from_dict({"auth": {"user": stored.username}}))
    calls = []
    token_session = SimpleNamespace(auth="mobile-token-auth")

    monkeypatch.setattr(cli, "token_store_from_config", lambda config, keyring: store)
    monkeypatch.setattr(
        cli.moodle_api,
        "validate_mobile_tokens",
        lambda value: valid(value),
    )

    def create_token_session(value, user_private_access_key):
        assert value is stored
        assert user_private_access_key == "download-key"
        return token_session

    monkeypatch.setattr(cli.moodle_api, "create_token_session", create_token_session)
    monkeypatch.setattr(
        cli.rwth, "login", lambda *args: pytest.fail("healthy sync performed SSO")
    )
    monkeypatch.setattr(cli.sync, "sync", lambda value: calls.append("sync"))
    monkeypatch.setattr(
        cli.downloader,
        "download_all_files",
        lambda value, log: calls.append("download"),
    )
    monkeypatch.setattr(
        cli.course_cache,
        "cache_root_node",
        lambda value, log: calls.append("cache"),
    )

    cli.run(ctx)

    assert calls == ["sync", "download", "cache"]
    assert ctx.session is token_session
    assert ctx.moodle_account == MoodleAccount(stored)
    assert store.writes == []


def test_unknown_token_validation_never_logs_in_or_replaces_store(monkeypatch):
    stored = tokens()
    store = MemoryStore(stored)
    ctx = SyncContext(Config.from_dict({"auth": {"user": stored.username}}))
    unknown = cli.moodle_api.TokenValidation(
        cli.moodle_api.TokenValidationKind.UNKNOWN, "offline"
    )
    monkeypatch.setattr(cli, "token_store_from_config", lambda config, keyring: store)
    monkeypatch.setattr(
        cli.moodle_api, "validate_mobile_tokens", lambda *args, **kwargs: unknown
    )
    monkeypatch.setattr(
        cli.rwth, "login", lambda *args: pytest.fail("network failure performed SSO")
    )

    with pytest.raises(SystemExit):
        cli.run(ctx)

    assert store.tokens == stored
    assert store.writes == []


def test_prompt_provider_requires_explicit_auth_login_for_invalid_tokens(
    monkeypatch, caplog
):
    stored = tokens()
    store = MemoryStore(stored)
    ctx = SyncContext(Config.from_dict({"auth": {"user": stored.username}}))
    invalid = cli.moodle_api.TokenValidation(cli.moodle_api.TokenValidationKind.INVALID)
    monkeypatch.setattr(cli, "token_store_from_config", lambda config, keyring: store)
    monkeypatch.setattr(
        cli.moodle_api, "validate_mobile_tokens", lambda *args, **kwargs: invalid
    )
    monkeypatch.setattr(
        cli.rwth, "login", lambda *args: pytest.fail("prompt provider performed SSO")
    )
    caplog.set_level(logging.CRITICAL, logger="syncmymoodle.cli")

    with pytest.raises(SystemExit) as exc_info:
        cli.run(ctx)

    assert exc_info.value.code == 1
    assert "requires interaction" in caplog.text
    assert "syncmymoodle auth login" in caplog.text
    assert store.writes == []


def test_reusable_provider_reauthenticates_once_and_stores_only_valid_replacement(
    monkeypatch,
):
    old = tokens()
    replacement = MoodleTokens(
        old.username,
        "replacement-ws",
        "replacement-private",
        moodle_user_id=old.moodle_user_id,
    )
    store = MemoryStore(old)
    ctx = SyncContext(
        Config.from_dict(
            {
                "auth": {
                    "user": old.username,
                    "login": {
                        "provider": "env-file",
                        "env_file": "/run/secrets/syncmymoodle-login.env",
                    },
                },
                "downloads": {"dry_run": True},
            }
        )
    )
    login_calls = []
    validations = iter(
        [
            cli.moodle_api.TokenValidation(cli.moodle_api.TokenValidationKind.INVALID),
            valid(replacement),
        ]
    )

    monkeypatch.setattr(cli, "token_store_from_config", lambda config, keyring: store)
    monkeypatch.setattr(
        cli.moodle_api,
        "validate_mobile_tokens",
        lambda *args, **kwargs: next(validations),
    )
    monkeypatch.setattr(
        cli.rwth,
        "login",
        lambda ctx, log, *, reuse_cached_session: login_calls.append(
            reuse_cached_session
        ),
    )
    monkeypatch.setattr(
        cli.moodle_api,
        "acquire_mobile_tokens",
        lambda session, username: replacement,
    )
    monkeypatch.setattr(
        cli.moodle_api,
        "create_token_session",
        lambda value, user_private_access_key: SimpleNamespace(),
    )
    monkeypatch.setattr(cli.sync, "sync", lambda value: None)
    monkeypatch.setattr(cli.downloader, "download_all_files", lambda value, log: None)

    cli.run(ctx)

    assert login_calls == [False]
    assert store.writes == [replacement]


def test_browser_bootstrap_is_attempted_at_most_once_per_run(monkeypatch):
    ctx = SyncContext(Config.from_dict({"auth": {"user": "ab123456"}}))
    ctx.moodle_account = MoodleAccount(tokens())
    attempts = []
    monkeypatch.setattr(
        cli.rwth,
        "cached_session_status",
        lambda path: cli.rwth.SessionStatus(cli.rwth.SessionStatusKind.EXPIRED),
    )

    def rate_limited(value):
        attempts.append(value)
        raise cli.moodle_api.BrowserBootstrapError(
            "Moodle browser auto-login is rate-limited; retry in up to 6 minutes"
        )

    monkeypatch.setattr(cli.moodle_api, "create_browser_session", rate_limited)
    cli.configure_browser_session_resolver(ctx)

    with pytest.raises(BrowserSessionUnavailable, match="rate-limited"):
        ctx.require_browser_session()
    with pytest.raises(BrowserSessionUnavailable):
        ctx.require_browser_session()

    assert attempts == [ctx.moodle_account.tokens]


def test_cached_browser_session_for_another_account_is_replaced(monkeypatch):
    ctx = SyncContext(
        Config.from_dict(
            {
                "auth": {"user": "ab123456"},
                "downloads": {"dry_run": True},
            }
        )
    )
    ctx.moodle_account = MoodleAccount(tokens())
    cached_session = SimpleNamespace()
    replacement_session = SimpleNamespace()
    replacements = []
    monkeypatch.setattr(
        cli.rwth,
        "cached_session_status",
        lambda path: cli.rwth.SessionStatus(cli.rwth.SessionStatusKind.VALID, 60),
    )
    monkeypatch.setattr(
        cli.rwth,
        "load_cached_session",
        lambda path: (cached_session, "cached-session-key"),
    )
    monkeypatch.setattr(
        cli.moodle_api,
        "browser_session_user_id",
        lambda session: 456,
    )

    def create_browser_session(value):
        replacements.append(value)
        return replacement_session, "replacement-session-key"

    monkeypatch.setattr(
        cli.moodle_api,
        "create_browser_session",
        create_browser_session,
    )
    cli.configure_browser_session_resolver(ctx)

    assert ctx.require_browser_session() is replacement_session
    assert ctx.browser_session_key == "replacement-session-key"
    assert replacements == [ctx.moodle_account.tokens]
