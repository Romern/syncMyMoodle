from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from syncmymoodle.storage import harden_private_file

KEYRING_SERVICE = "syncmymoodle"
ENV_FILE_PASSWORD_KEY = "SYNCMYMOODLE_PASSWORD"
ENV_FILE_TOTP_SECRET_KEY = "SYNCMYMOODLE_TOTP_SECRET"
SECRET_COMMAND_TIMEOUT_SECONDS = 300
UNUSABLE_KEYRING_BACKENDS = {
    ("keyring.backends.fail", "Keyring"),
    ("keyring.backends.null", "Keyring"),
}


@dataclass(frozen=True)
class ProviderAvailability:
    available: bool
    reason: str | None = None


class ProviderSecretError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)


CommandRunner = Callable[[tuple[str, ...]], CommandResult]
ExecutableFinder = Callable[[str], str | None]


class SecretProvider(Protocol):
    def check_available(self) -> ProviderAvailability: ...

    def get_password(self, reference: str) -> str | None: ...

    def get_otp_code(self, reference: str) -> str | None: ...


class KeyringProvider:
    def __init__(
        self,
        keyring_backend: Any,
        service_name: str = KEYRING_SERVICE,
    ) -> None:
        self.keyring_backend = keyring_backend
        self.service_name = service_name

    def check_available(self) -> ProviderAvailability:
        if self.keyring_backend is None:
            return ProviderAvailability(False, "no keyring backend was provided")
        if not callable(getattr(self.keyring_backend, "get_password", None)):
            return ProviderAvailability(False, "keyring backend cannot read secrets")
        if not callable(getattr(self.keyring_backend, "set_password", None)):
            return ProviderAvailability(False, "keyring backend cannot store secrets")

        get_keyring = getattr(self.keyring_backend, "get_keyring", None)
        if not callable(get_keyring):
            return ProviderAvailability(False, "keyring backend cannot be inspected")
        try:
            selected_backend = get_keyring()
        except Exception as exc:
            return ProviderAvailability(
                False,
                f"could not inspect keyring backend: {exc}",
            )

        backend_id = (
            type(selected_backend).__module__,
            type(selected_backend).__name__,
        )
        if backend_id in UNUSABLE_KEYRING_BACKENDS:
            return ProviderAvailability(False, "selected keyring backend is unusable")
        return ProviderAvailability(True)

    def get_secret(self, reference: str) -> str | None:
        try:
            secret = self.keyring_backend.get_password(self.service_name, reference)
        except Exception as exc:
            raise ProviderSecretError(f"could not read system keyring: {exc}") from exc
        return secret if isinstance(secret, str) else None

    def store_secret(self, reference: str, secret: str) -> None:
        try:
            self.keyring_backend.set_password(self.service_name, reference, secret)
        except Exception as exc:
            raise ProviderSecretError(f"could not write system keyring: {exc}") from exc

    def delete_secret(self, reference: str) -> None:
        delete_password = getattr(self.keyring_backend, "delete_password", None)
        if not callable(delete_password):
            raise ProviderSecretError("system keyring cannot delete secrets")
        try:
            delete_password(self.service_name, reference)
        except Exception as exc:
            raise ProviderSecretError(
                f"could not delete system keyring secret: {exc}"
            ) from exc


@dataclass(frozen=True)
class EnvFileCredentials:
    password: str | None = field(repr=False)
    totp_secret: str | None = field(repr=False)


def read_secure_env_file(path: Path, description: str) -> dict[str, str]:
    path = path.expanduser()
    if not path.exists():
        raise ProviderSecretError(f"{description} does not exist: {path}")
    if not path.is_file():
        raise ProviderSecretError(f"{description} path is not a file: {path}")
    if not harden_private_file(path, description):
        raise ProviderSecretError(f"{description} is not safe to read: {path}")
    try:
        return read_env_file(path)
    except OSError as exc:
        raise ProviderSecretError(f"could not read {description}: {exc}") from exc
    except ValueError as exc:
        raise ProviderSecretError(str(exc)) from exc


class EnvFileProvider:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def load_credentials(self) -> EnvFileCredentials:
        values = read_secure_env_file(self.path, "environment file")
        return EnvFileCredentials(
            values.get(ENV_FILE_PASSWORD_KEY),
            values.get(ENV_FILE_TOTP_SECRET_KEY),
        )


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not key.isidentifier():
            raise ValueError(f"invalid env secret file line {line_number}")
        values[key] = unquote_env_value(raw_value.strip(), line_number)
    return values


def unquote_env_value(value: str, line_number: int) -> str:
    if not value:
        return ""
    quote = value[0]
    if quote not in {"'", '"'}:
        return value
    if len(value) < 2 or value[-1] != quote:
        raise ValueError(f"unterminated quoted env secret on line {line_number}")
    return value[1:-1]


class ExternalCliProvider:
    def __init__(
        self,
        provider_name: str,
        binary: str,
        password_command: Callable[[str], tuple[str, ...]],
        otp_command: Callable[[str], tuple[str, ...]] | None = None,
        *,
        runner: CommandRunner | None = None,
        executable_finder: ExecutableFinder = shutil.which,
        password_first_line: bool = False,
        availability_check: Callable[["ExternalCliProvider"], ProviderAvailability]
        | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.binary = binary
        self.password_command = password_command
        self.otp_command = otp_command
        self.runner = run_cli_command if runner is None else runner
        self.executable_finder = executable_finder
        self.password_first_line = password_first_line
        self.availability_check = availability_check

    def is_installed(self) -> bool:
        return self.executable_finder(self.binary) is not None

    def check_available(self) -> ProviderAvailability:
        if not self.is_installed():
            return ProviderAvailability(False, f"{self.binary!r} executable not found")
        if self.availability_check is not None:
            return self.availability_check(self)
        return ProviderAvailability(True)

    def get_password(self, reference: str) -> str | None:
        return self._run_secret_command(
            self.password_command(reference),
            first_line=self.password_first_line,
        )

    def get_otp_code(self, reference: str) -> str | None:
        if self.otp_command is None:
            return None
        return self._run_secret_command(self.otp_command(reference), first_line=True)

    def _run_secret_command(
        self,
        argv: tuple[str, ...],
        *,
        first_line: bool = False,
    ) -> str:
        return run_secret_command(
            self.provider_name,
            self.runner,
            argv,
            first_line=first_line,
        )


@dataclass(frozen=True)
class ExternalProviderSpec:
    name: str
    display_name: str
    binary: str
    password_command: tuple[str, ...]
    otp_command: tuple[str, ...]
    password_example: str
    otp_example: str
    password_first_line: bool = False
    availability_check: Callable[[ExternalCliProvider], ProviderAvailability] | None = (
        None
    )


def run_cli_command(argv: tuple[str, ...]) -> CommandResult:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=SECRET_COMMAND_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        raise ProviderSecretError(f"could not run {argv[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderSecretError(
            f"{argv[0]!r} timed out after {SECRET_COMMAND_TIMEOUT_SECONDS} seconds"
        ) from exc
    return CommandResult(result.returncode, result.stdout, result.stderr)


def sanitize_command_error(stderr: str) -> str:
    if not stderr.strip():
        return "command exited without an error message"
    return "provider error output was suppressed"


def first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        value = line.strip()
        if value:
            return value
    return ""


def build_external_secret_provider(
    provider_name: str,
    *,
    runner: CommandRunner | None = None,
    executable_finder: ExecutableFinder = shutil.which,
) -> ExternalCliProvider:
    runner = run_cli_command if runner is None else runner
    spec = get_external_secret_provider_spec(provider_name)
    return ExternalCliProvider(
        spec.name,
        spec.binary,
        lambda reference: (*spec.password_command, reference),
        lambda reference: (*spec.otp_command, reference),
        runner=runner,
        executable_finder=executable_finder,
        password_first_line=spec.password_first_line,
        availability_check=spec.availability_check,
    )


def detect_password_manager_clis(
    executable_finder: ExecutableFinder = shutil.which,
) -> tuple[str, ...]:
    return tuple(
        provider_name
        for provider_name in EXTERNAL_SECRET_PROVIDER_OPTIONS
        if build_external_secret_provider(
            provider_name,
            executable_finder=executable_finder,
        ).is_installed()
    )


class CommandSecretProvider:
    def __init__(
        self,
        password_command: tuple[str, ...],
        otp_command: tuple[str, ...] = (),
        *,
        runner: CommandRunner | None = None,
        executable_finder: ExecutableFinder = shutil.which,
    ) -> None:
        self.password_command = password_command
        self.otp_command = otp_command
        self.runner = run_cli_command if runner is None else runner
        self.executable_finder = executable_finder

    def check_available(self) -> ProviderAvailability:
        return self._check_command(self.password_command, "password_command")

    def check_otp_available(self) -> ProviderAvailability:
        return self._check_command(self.otp_command, "otp_command")

    def _check_command(
        self, command: tuple[str, ...], setting_name: str
    ) -> ProviderAvailability:
        if not command:
            return ProviderAvailability(False, f"{setting_name} is required")
        executable = command[0]
        if self.executable_finder(executable) is None:
            return ProviderAvailability(False, f"{executable!r} executable not found")
        return ProviderAvailability(True)

    def get_password(self, reference: str = "") -> str | None:
        del reference
        return run_secret_command("command", self.runner, self.password_command)

    def get_otp_code(self, reference: str = "") -> str | None:
        del reference
        if not self.otp_command:
            return None
        return run_secret_command("command", self.runner, self.otp_command)


def run_secret_command(
    provider_name: str,
    runner: CommandRunner,
    argv: tuple[str, ...],
    *,
    first_line: bool = False,
) -> str:
    result = runner(argv)
    if result.returncode != 0:
        raise ProviderSecretError(
            f"{provider_name} command failed: {sanitize_command_error(result.stderr)}"
        )
    secret = first_nonempty_line(result.stdout) if first_line else result.stdout.strip()
    if not secret:
        raise ProviderSecretError(f"{provider_name} command returned no secret")
    return secret


def check_bitwarden_available(provider: ExternalCliProvider) -> ProviderAvailability:
    result = provider.runner(("bw", "status"))
    if result.returncode != 0:
        return ProviderAvailability(
            False,
            f"bw status failed: {sanitize_command_error(result.stderr)}",
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ProviderAvailability(False, "bw status returned invalid JSON")
    if not isinstance(payload, dict):
        return ProviderAvailability(False, "bw status returned unexpected JSON")
    status = payload.get("status")
    if status == "unlocked":
        return ProviderAvailability(True)
    if status == "locked":
        return ProviderAvailability(False, "Bitwarden vault is locked; unlock it first")
    if status == "unauthenticated":
        return ProviderAvailability(False, "Bitwarden CLI is not logged in")
    return ProviderAvailability(False, f"Bitwarden status is {status!r}")


EXTERNAL_SECRET_PROVIDER_SPECS = {
    spec.name: spec
    for spec in (
        ExternalProviderSpec(
            "1password",
            "1Password",
            "op",
            ("op", "read"),
            ("op", "read"),
            "op://Private/RWTH/password",
            "op://Private/RWTH/one-time password?attribute=otp",
        ),
        ExternalProviderSpec(
            "bitwarden",
            "Bitwarden",
            "bw",
            ("bw", "get", "password"),
            ("bw", "get", "totp"),
            "rwth/sso",
            "rwth/sso",
            availability_check=check_bitwarden_available,
        ),
        ExternalProviderSpec(
            "pass",
            "pass",
            "pass",
            ("pass", "show"),
            ("pass", "otp"),
            "rwth/sso",
            "rwth/sso",
            password_first_line=True,
        ),
        ExternalProviderSpec(
            "rbw",
            "rbw",
            "rbw",
            ("rbw", "get"),
            ("rbw", "code"),
            "rwth/sso",
            "rwth/sso",
        ),
        ExternalProviderSpec(
            "gopass",
            "gopass",
            "gopass",
            ("gopass", "show", "--password"),
            ("gopass", "otp", "--password"),
            "rwth/sso",
            "rwth/sso",
        ),
    )
}
EXTERNAL_SECRET_PROVIDER_OPTIONS = tuple(EXTERNAL_SECRET_PROVIDER_SPECS)
SECRET_PROVIDER_OPTIONS = (*EXTERNAL_SECRET_PROVIDER_OPTIONS, "command")


def get_external_secret_provider_spec(provider_name: str) -> ExternalProviderSpec:
    try:
        return EXTERNAL_SECRET_PROVIDER_SPECS[provider_name]
    except KeyError:
        raise ValueError(
            f"unknown external secret provider {provider_name!r}"
        ) from None
