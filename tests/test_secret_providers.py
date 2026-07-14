import subprocess
from types import SimpleNamespace

import pytest

import syncmymoodle.secret_providers as secret_providers
from syncmymoodle.secret_providers import (
    ENV_FILE_PASSWORD_KEY,
    ENV_FILE_TOTP_SECRET_KEY,
    KEYRING_SERVICE,
    SECRET_COMMAND_TIMEOUT_SECONDS,
    CommandResult,
    CommandSecretProvider,
    EnvFileProvider,
    KeyringProvider,
    ProviderSecretError,
    build_external_secret_provider,
    detect_password_manager_clis,
    read_env_file,
    run_cli_command,
)

from .helpers import FakeKeyring


def test_keyring_provider_is_available_for_regular_backend():
    provider = KeyringProvider(FakeKeyring())

    availability = provider.check_available()

    assert availability.available
    assert availability.reason is None


@pytest.mark.parametrize(
    "module_name",
    ["keyring.backends.fail", "keyring.backends.null"],
)
def test_keyring_provider_rejects_known_dummy_backends(module_name):
    fake_backend = type("Keyring", (), {"__module__": module_name})()
    fake_keyring = SimpleNamespace(
        get_keyring=lambda: fake_backend,
        get_password=lambda service, name: None,
        set_password=lambda service, name, value: None,
    )
    provider = KeyringProvider(fake_keyring)

    availability = provider.check_available()

    assert not availability.available
    assert availability.reason == "selected keyring backend is unusable"


def test_keyring_provider_reads_and_writes_secrets():
    stored: dict[tuple[str, str], str] = {}
    provider = KeyringProvider(FakeKeyring(stored))

    provider.store_secret("user", "password")

    assert stored == {(KEYRING_SERVICE, "user"): "password"}
    assert provider.get_secret("user") == "password"


def test_read_env_file_parses_comments_export_and_quotes(tmp_path):
    env_path = tmp_path / "secrets.env"
    env_path.write_text(
        """
# comment
export SYNCMYMOODLE_PASSWORD='env-password'
SYNCMYMOODLE_TOTP_SECRET="totp=secret"
EMPTY=
""",
        encoding="utf-8",
    )

    assert read_env_file(env_path) == {
        ENV_FILE_PASSWORD_KEY: "env-password",
        ENV_FILE_TOTP_SECRET_KEY: "totp=secret",
        "EMPTY": "",
    }


def test_read_env_file_rejects_invalid_lines(tmp_path):
    env_path = tmp_path / "secrets.env"
    env_path.write_text("not a valid env line\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid env secret file line 1"):
        read_env_file(env_path)


def test_env_file_provider_reads_password_and_totp_secret(tmp_path):
    env_path = tmp_path / "secrets.env"
    env_path.write_text(
        "SYNCMYMOODLE_PASSWORD=password\nSYNCMYMOODLE_TOTP_SECRET=totp-secret\n",
        encoding="utf-8",
    )
    provider = EnvFileProvider(env_path)

    credentials = provider.load_credentials()

    assert credentials.password == "password"
    assert credentials.totp_secret == "totp-secret"
    assert "password" not in repr(credentials)
    assert "totp-secret" not in repr(credentials)


def test_env_file_provider_reports_missing_file(tmp_path):
    provider = EnvFileProvider(tmp_path / "missing.env")

    with pytest.raises(ProviderSecretError, match="does not exist"):
        provider.load_credentials()


def test_env_file_provider_fails_closed_when_permissions_cannot_be_hardened(
    tmp_path, monkeypatch
):
    env_path = tmp_path / "secrets.env"
    env_path.write_text("SYNCMYMOODLE_PASSWORD=password\n", encoding="utf-8")
    monkeypatch.setattr(
        "syncmymoodle.storage.chmod_private_best_effort",
        lambda path, description: False,
    )

    with pytest.raises(ProviderSecretError, match="not safe to read"):
        EnvFileProvider(env_path).load_credentials()


def test_1password_provider_reads_password_and_otp_with_fixed_op_commands():
    calls = []

    def runner(argv):
        calls.append(argv)
        return CommandResult(0, f"secret for {argv[-1]}\n", "")

    provider = build_external_secret_provider(
        "1password",
        runner=runner,
        executable_finder=lambda binary: f"/usr/bin/{binary}",
    )

    assert provider.check_available().available
    assert provider.get_password("op://vault/item/password") == (
        "secret for op://vault/item/password"
    )
    assert provider.get_otp_code("op://vault/item/otp?attribute=otp") == (
        "secret for op://vault/item/otp?attribute=otp"
    )
    assert calls == [
        ("op", "read", "op://vault/item/password"),
        ("op", "read", "op://vault/item/otp?attribute=otp"),
    ]


def test_pass_provider_uses_first_output_line_for_password():
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[:2] == ("pass", "show"):
            return CommandResult(0, "password\nmetadata: ignored\n", "")
        return CommandResult(0, "123456\n", "")

    provider = build_external_secret_provider(
        "pass",
        runner=runner,
        executable_finder=lambda binary: f"/usr/bin/{binary}",
    )

    assert provider.get_password("rwth/sso") == "password"
    assert provider.get_otp_code("rwth/sso") == "123456"
    assert calls == [
        ("pass", "show", "rwth/sso"),
        ("pass", "otp", "rwth/sso"),
    ]


def test_bitwarden_provider_requires_unlocked_vault():
    provider = build_external_secret_provider(
        "bitwarden",
        runner=lambda argv: CommandResult(0, '{"status":"locked"}', ""),
        executable_finder=lambda binary: f"/usr/bin/{binary}",
    )

    availability = provider.check_available()

    assert not availability.available
    assert "locked" in str(availability.reason)


def test_bitwarden_provider_reports_unexpected_status_shape():
    provider = build_external_secret_provider(
        "bitwarden",
        runner=lambda argv: CommandResult(0, "[]", ""),
        executable_finder=lambda binary: f"/usr/bin/{binary}",
    )

    availability = provider.check_available()

    assert not availability.available
    assert availability.reason == "bw status returned unexpected JSON"


def test_bitwarden_provider_reads_password_and_otp_when_unlocked():
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv == ("bw", "status"):
            return CommandResult(0, '{"status":"unlocked"}', "")
        if argv[:3] == ("bw", "get", "password"):
            return CommandResult(0, "bw-password\n", "")
        return CommandResult(0, "654321\n", "")

    provider = build_external_secret_provider(
        "bitwarden",
        runner=runner,
        executable_finder=lambda binary: f"/usr/bin/{binary}",
    )

    assert provider.check_available().available
    assert provider.get_password("rwth item") == "bw-password"
    assert provider.get_otp_code("rwth item") == "654321"
    assert calls == [
        ("bw", "status"),
        ("bw", "get", "password", "rwth item"),
        ("bw", "get", "totp", "rwth item"),
    ]


def test_rbw_provider_reads_password_and_otp_with_fixed_commands():
    calls = []

    def runner(argv):
        calls.append(argv)
        return CommandResult(0, "secret\n", "")

    provider = build_external_secret_provider(
        "rbw",
        runner=runner,
        executable_finder=lambda binary: f"/usr/bin/{binary}",
    )

    assert provider.get_password("rwth") == "secret"
    assert provider.get_otp_code("rwth") == "secret"
    assert calls == [
        ("rbw", "get", "rwth"),
        ("rbw", "code", "rwth"),
    ]


def test_gopass_provider_reads_password_and_otp_with_fixed_commands():
    calls = []

    def runner(argv):
        calls.append(argv)
        return CommandResult(0, "secret\n", "")

    provider = build_external_secret_provider(
        "gopass",
        runner=runner,
        executable_finder=lambda binary: f"/usr/bin/{binary}",
    )

    assert provider.get_password("rwth/sso") == "secret"
    assert provider.get_otp_code("rwth/sso") == "secret"
    assert calls == [
        ("gopass", "show", "--password", "rwth/sso"),
        ("gopass", "otp", "--password", "rwth/sso"),
    ]


def test_password_manager_detection_only_checks_supported_cli_executables(monkeypatch):
    checked = []

    def find_executable(binary):
        checked.append(binary)
        return f"/tools/{binary}" if binary in {"op", "bw", "rbw"} else None

    monkeypatch.setattr(
        secret_providers,
        "run_cli_command",
        lambda argv: pytest.fail(f"detection must not run {argv}"),
    )

    assert detect_password_manager_clis(find_executable) == (
        "1password",
        "bitwarden",
        "rbw",
    )
    assert checked == ["op", "bw", "pass", "rbw", "gopass"]


def test_external_provider_command_failure_does_not_include_command_output():
    provider = build_external_secret_provider(
        "1password",
        runner=lambda argv: CommandResult(1, "stdout-secret", "stderr-secret\n"),
        executable_finder=lambda binary: f"/usr/bin/{binary}",
    )

    with pytest.raises(ProviderSecretError) as exc_info:
        provider.get_password("op://vault/missing/password")

    message = str(exc_info.value)
    assert "provider error output was suppressed" in message
    assert "stdout-secret" not in message
    assert "stderr-secret" not in message


def test_run_cli_command_sets_bounded_timeout(monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="secret\n", stderr="")

    monkeypatch.setattr("syncmymoodle.secret_providers.subprocess.run", fake_run)

    result = run_cli_command(("secret-tool", "lookup", "rwth"))

    assert result == CommandResult(0, "secret\n", "")
    assert calls["argv"] == ("secret-tool", "lookup", "rwth")
    assert calls["kwargs"]["timeout"] == SECRET_COMMAND_TIMEOUT_SECONDS


def test_command_result_repr_does_not_expose_captured_secrets():
    result = CommandResult(1, "stdout-secret", "stderr-secret")

    assert "stdout-secret" not in repr(result)
    assert "stderr-secret" not in repr(result)


def test_run_cli_command_reports_timeout_without_command_output(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv,
            kwargs["timeout"],
            output="stdout-secret",
            stderr="stderr-secret",
        )

    monkeypatch.setattr("syncmymoodle.secret_providers.subprocess.run", fake_run)

    with pytest.raises(ProviderSecretError) as exc_info:
        run_cli_command(("secret-tool", "lookup", "rwth"))

    message = str(exc_info.value)
    assert f"timed out after {SECRET_COMMAND_TIMEOUT_SECONDS} seconds" in message
    assert "stdout-secret" not in message
    assert "stderr-secret" not in message


def test_command_provider_runs_pre_split_argv_without_shell():
    calls = []

    def runner(argv):
        calls.append(argv)
        return CommandResult(0, "secret\n", "")

    provider = CommandSecretProvider(
        ("secret-tool", "lookup", "rwth"),
        ("otp-tool", "code", "rwth"),
        runner=runner,
        executable_finder=lambda binary: f"/usr/bin/{binary}",
    )

    assert provider.check_available().available
    assert provider.get_password() == "secret"
    assert provider.get_otp_code() == "secret"
    assert calls == [
        ("secret-tool", "lookup", "rwth"),
        ("otp-tool", "code", "rwth"),
    ]


def test_command_provider_requires_password_command_executable():
    provider = CommandSecretProvider(
        ("missing-tool", "lookup", "rwth"),
        executable_finder=lambda binary: None,
    )

    availability = provider.check_available()

    assert not availability.available
    assert availability.reason == "'missing-tool' executable not found"


def test_command_provider_checks_password_and_otp_executables_separately():
    provider = CommandSecretProvider(
        ("password-tool", "read"),
        ("missing-otp-tool", "code"),
        executable_finder=lambda binary: (
            "/usr/bin/password-tool" if binary == "password-tool" else None
        ),
    )

    assert provider.check_available().available
    otp_availability = provider.check_otp_available()
    assert not otp_availability.available
    assert otp_availability.reason == "'missing-otp-tool' executable not found"
