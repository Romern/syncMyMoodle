#!/usr/bin/env python3

import getpass
import json
import logging
import sys
import tomllib
from argparse import (
    SUPPRESS,
    Action,
    ArgumentParser,
    BooleanOptionalAction,
    Namespace,
)
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata, resources
from itertools import groupby
from pathlib import Path
from typing import Any

import tomlkit

from syncmymoodle import cleanup, course_cache, downloader, pathing, rwth, sync
from syncmymoodle import moodle as moodle_api
from syncmymoodle.config import (
    CONFIG_OPTIONS,
    CommandAuthSource,
    Config,
    ConfigDict,
    ConfigValidationError,
    EnvFileAuthSource,
    ExternalAuthSource,
    KeyringAuthSource,
    PromptAuthSource,
    canonicalize,
    config_validation_errors,
    convert_legacy_config,
    group_config_for_toml,
    literal_dotted_toml_key_errors,
    resolve_relative_path_options,
)
from syncmymoodle.constants import COURSE_CACHE_FILENAME, MOODLE_NETLOC
from syncmymoodle.context import (
    AuthState,
    BrowserSessionUnavailable,
    MoodleAccount,
    SyncContext,
)
from syncmymoodle.moodle_tokens import (
    EnvFileTokenStore,
    KeyringTokenStore,
    MoodleTokens,
    MoodleTokenStore,
    store_tokens_verified,
    token_store_transaction,
)
from syncmymoodle.secret_providers import (
    CommandSecretProvider,
    EnvFileProvider,
    KeyringProvider,
    ProviderAvailability,
    ProviderSecretError,
    SecretProvider,
    build_external_secret_provider,
    detect_password_manager_clis,
)
from syncmymoodle.storage import save_session, write_private_text

logger = logging.getLogger(__name__)
CONFIG_TOML_FILENAME = "config.toml"
CONFIG_JSON_FILENAME = "config.json"
STARTER_CONFIG_RESOURCE = "config.toml.example"
keyring: Any = None


class DeprecatedAliasAction(Action):
    def __init__(
        self,
        option_strings: Sequence[str],
        dest: str,
        **kwargs: Any,
    ) -> None:
        self.replacement = str(kwargs.pop("replacement"))
        super().__init__(option_strings, dest, **kwargs)

    def __call__(
        self,
        parser: ArgumentParser,
        namespace: Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        del parser
        assert option_string is not None
        print(
            f"warning: {option_string} is deprecated; use {self.replacement} instead",
            file=sys.stderr,
        )
        setattr(namespace, self.dest, self.const if self.nargs == 0 else values)


def load_keyring_backend() -> Any:
    global keyring
    if keyring is not None:
        return keyring
    try:
        import keyring as imported_keyring
    except ImportError:
        return None
    keyring = imported_keyring
    return keyring


def package_version() -> str:
    try:
        return metadata.version("syncmymoodle")
    except metadata.PackageNotFoundError:
        return "unknown"


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="syncmymoodle",
        allow_abbrev=False,
        description=(
            "Run without a subcommand to sync RWTH Moodle. The sync options below "
            "override values from the selected configuration file."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help="use this configuration file instead of the global configuration",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"syncmymoodle {package_version()}",
    )
    cli_groups = {
        group: parser.add_argument_group(group)
        for group in dict.fromkeys(option.group for option in CONFIG_OPTIONS)
    }
    for option in CONFIG_OPTIONS:
        cli = option.cli
        if cli is None:
            continue
        argument_group = cli_groups[option.group]
        kwargs: dict[str, Any] = {}
        if cli.value_kind == "flag":
            kwargs["action"] = BooleanOptionalAction
        elif option.choices:
            kwargs["choices"] = option.choices
        argument_group.add_argument(
            f"--{cli.arg_name}",
            help=cli.help,
            **kwargs,
        )
        if cli.aliases:
            replacement = (
                f"--no-{cli.arg_name}"
                if cli.value_kind == "flag" and not cli.legacy_flag_value
                else f"--{cli.arg_name}"
            )
            alias_kwargs: dict[str, Any] = {
                "action": DeprecatedAliasAction,
                "replacement": replacement,
            }
            if cli.value_kind == "flag":
                alias_kwargs.update(nargs=0, const=cli.legacy_flag_value)
            elif option.choices:
                alias_kwargs["choices"] = option.choices
            argument_group.add_argument(
                *(f"--{alias}" for alias in cli.aliases),
                dest=cli.dest,
                default=SUPPRESS,
                help=SUPPRESS,
                **alias_kwargs,
            )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_const",
        dest="loglevel",
        const=logging.INFO,
        default=logging.WARNING,
        help="show information useful for debugging",
    )
    parser.add_argument(
        "--show-filtered",
        action="store_true",
        help="list files, courses, and other items excluded by configured filters",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "setup",
        help="configure and verify syncMyMoodle for first use",
        description=(
            "Interactively configure RWTH sign-in, secure Moodle token storage, "
            "and the sync destination, then verify the RWTH sign-in with one login."
        ),
    )
    config_parser = subparsers.add_parser("config", help="manage configuration files")
    config_subparsers = config_parser.add_subparsers(
        dest="config_command",
        required=True,
    )
    config_subparsers.add_parser(
        "example",
        help="print a complete, commented example configuration",
    )
    config_subparsers.add_parser(
        "path",
        help="show the global config location",
    )
    migrate_parser = config_subparsers.add_parser(
        "migrate",
        help="convert a legacy JSON config file to TOML",
    )
    migrate_parser.add_argument(
        "--input",
        default=None,
        help="legacy JSON config to migrate; defaults to the global config.json",
    )
    migrate_parser.add_argument(
        "--output",
        default=None,
        help="TOML output path; defaults to the input path with a .toml suffix",
    )
    migrate_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite the TOML output file if it already exists",
    )
    migrate_parser.add_argument(
        "--token-store",
        choices=("keyring", "env-file"),
        default="keyring",
        help="secure store for the migrated Moodle tokens",
    )
    migrate_parser.add_argument(
        "--token-env-file",
        default=None,
        help=(
            "protected environment file for Moodle tokens when --token-store "
            "is env-file"
        ),
    )
    config_subparsers.add_parser(
        "check",
        help="validate a configuration file",
        description=(
            "Validate the global configuration, or select another file with "
            "`syncmymoodle --config PATH config check`."
        ),
    )
    auth_parser = subparsers.add_parser("auth", help="manage authentication")
    auth_subparsers = auth_parser.add_subparsers(
        dest="auth_command",
        required=True,
    )
    auth_login_parser = auth_subparsers.add_parser(
        "login",
        help="log in once and replace the stored Moodle tokens",
        description=(
            "Perform one fresh RWTH sign-in and replace this installation's "
            "stored Moodle tokens. This does not revoke the shared Moodle API token."
        ),
    )
    auth_login_parser.add_argument(
        "--totp-manual",
        action="store_true",
        help="ignore the configured TOTP source and prompt for a code for this login",
    )
    auth_migrate_parser = auth_subparsers.add_parser(
        "migrate",
        help="copy Moodle tokens to another secure store and update the configuration",
        description=(
            "Copy the stored Moodle tokens to another secure store and update the "
            "selected configuration. The previous store is left untouched."
        ),
    )
    auth_migrate_parser.add_argument(
        "--to",
        choices=("keyring", "env-file"),
        required=True,
        help="secure store to use for Moodle tokens",
    )
    auth_migrate_parser.add_argument(
        "--env-file",
        default=None,
        help="destination protected environment file when --to is env-file",
    )
    auth_subparsers.add_parser(
        "status",
        help="show Moodle token and cached browser-session status",
        description=(
            "Read and validate the stored Moodle tokens, check the configured RWTH "
            "sign-in method, and report the cached browser session without signing in."
        ),
    )
    auth_subparsers.add_parser(
        "forget",
        help="remove local Moodle tokens and the cached browser session",
        description=(
            "Remove this installation's Moodle tokens and cached browser session. "
            "The shared Moodle API token, configuration, and RWTH sign-in secrets remain."
        ),
    )
    auth_subparsers.add_parser(
        "reset-token",
        help="explicitly reset the shared Moodle API token",
        description=(
            "Revoke and replace the shared Moodle API token. This also invalidates "
            "the Moodle app and every other syncMyMoodle installation using it."
        ),
    )
    clean_parser = subparsers.add_parser(
        "clean",
        help="inspect and clean local sync artifacts; dry-run by default",
    )
    clean_subparsers = clean_parser.add_subparsers(
        dest="clean_command",
        required=True,
    )
    conflicts_parser = clean_subparsers.add_parser(
        "conflicts",
        help="preview removal of redundant .syncconflict files",
        description=(
            "Find redundant .syncconflict files. The default is a dry run; pass "
            "--apply to delete only the files listed as redundant."
        ),
    )
    add_clean_path_apply_options(
        conflicts_parser,
        "actually delete redundant conflict files",
    )
    caches_parser = clean_subparsers.add_parser(
        "caches",
        help="preview a reset of per-course metadata caches; rarely needed",
        description=(
            f"Find per-course {COURSE_CACHE_FILENAME} metadata files. The default "
            "is a dry run; pass --apply to delete them. The next sync will rebuild "
            "the caches and may do extra work."
        ),
    )
    add_clean_path_apply_options(caches_parser, "actually delete cache files")
    return parser


def add_clean_path_apply_options(
    subparser: ArgumentParser,
    apply_help: str,
) -> None:
    subparser.add_argument(
        "--path",
        default=None,
        help="directory to scan; defaults to paths.sync_directory",
    )
    subparser.add_argument(
        "--apply",
        action="store_true",
        help=apply_help,
    )


@dataclass(frozen=True)
class LoadedConfig:
    values: ConfigDict = field(repr=False)
    text: str = field(repr=False)


def legacy_json_migration_message(path: Path) -> str:
    return (
        f"found legacy JSON config: {path}; migrate it with "
        f"`syncmymoodle config migrate --input {path}` first"
    )


def read_config_file_unresolved(path: Path) -> LoadedConfig:
    """Parse and canonicalize a single config file without resolving paths.

    Validation happens after relative paths are resolved by the caller.
    """
    path = pathing.absolute_path(path)
    text = path.read_text(encoding="utf-8")
    return parse_config_text(path, text)


def parse_config_text(path: Path, text: str) -> LoadedConfig:
    """Parse current-format TOML config text."""
    try:
        values = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        try:
            legacy_values: Any = json.loads(text)
        except json.JSONDecodeError:
            legacy_values = None
        if isinstance(legacy_values, Mapping):
            raise ValueError(legacy_json_migration_message(path)) from error
        raise
    structure_errors = literal_dotted_toml_key_errors(values)
    if structure_errors:
        raise ConfigValidationError(path, structure_errors)
    return LoadedConfig(canonicalize(values), text)


def read_legacy_config_file(path: Path) -> tuple[Mapping[str, Any], ConfigDict]:
    """Read legacy JSON exclusively for the explicit migration command."""
    path = pathing.absolute_path(path)
    try:
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(
            f"migration input must be a legacy JSON config file: {path}"
        ) from error
    if not isinstance(parsed, Mapping):
        raise ValueError(
            f"legacy config root must be an object, got {type(parsed).__name__}"
        )
    return parsed, canonicalize(convert_legacy_config(parsed))


def read_config_file(path: Path) -> ConfigDict:
    path = pathing.absolute_path(path)
    loaded = read_config_file_unresolved(path)
    resolved = resolve_relative_path_options(loaded.values, path.parent)
    errors = config_validation_errors(resolved, config_path=path)
    if errors:
        raise ConfigValidationError(path, errors)
    return resolved


def global_config_path() -> Path:
    return pathing.user_config_dir() / CONFIG_TOML_FILENAME


def discover_config_file(directory: Path) -> Path | None:
    path = directory / CONFIG_TOML_FILENAME
    return path if path.is_file() else None


def discover_json_migration_input() -> Path | None:
    global_config = pathing.user_config_dir() / CONFIG_JSON_FILENAME
    return global_config if global_config.is_file() else None


def overlay_config_values(
    target: MutableMapping[str, Any],
    overrides: Mapping[str, Any],
) -> None:
    for key, value in overrides.items():
        if isinstance(value, Mapping):
            table = target.get(key)
            if not isinstance(table, MutableMapping):
                raise ValueError(f"starter config does not define table {key!r}")
            overlay_config_values(table, value)
        else:
            if key not in target:
                raise ValueError(f"starter config does not define option {key!r}")
            target[key] = value


def starter_config_text(overrides: Mapping[str, Any] | None = None) -> str:
    text = (
        resources.files("syncmymoodle")
        .joinpath(STARTER_CONFIG_RESOURCE)
        .read_text(encoding="utf-8")
    )
    if not overrides:
        return text
    document = tomlkit.parse(text)
    overlay_config_values(document, group_config_for_toml(overrides))
    return tomlkit.dumps(document)


def migrated_config_text(values: Mapping[str, Any], baseline: Config) -> str:
    """Render migrated values with behavior-compatible example defaults."""
    text = starter_config_text()
    example_values = canonicalize(tomllib.loads(text))
    output_values = dict(canonicalize(values))
    for option in CONFIG_OPTIONS:
        key = option.canonical_key
        if key in output_values or key not in example_values:
            continue
        candidate = {**output_values, key: example_values[key]}
        try:
            unchanged = Config.from_dict(candidate) == baseline
        except ConfigValidationError:
            unchanged = False
        if unchanged:
            output_values[key] = example_values[key]

    document = tomlkit.parse(text)
    for option in CONFIG_OPTIONS:
        if option.canonical_key in output_values:
            continue
        table: MutableMapping[str, Any] = document
        for group_part in option.group.split("."):
            nested = table.get(group_part)
            if not isinstance(nested, MutableMapping):
                raise ValueError(
                    f"starter config does not define table {option.group!r}"
                )
            table = nested
        table.pop(option.key, None)
    overlay_config_values(document, group_config_for_toml(output_values))
    return tomlkit.dumps(document)


def selected_config_path(
    args: Namespace,
    parser: ArgumentParser,
    *,
    required: bool = False,
) -> Path | None:
    if args.config:
        explicit_config = pathing.absolute_path(Path(args.config))
        if not explicit_config.is_file():
            # Silently continuing without the explicitly requested file would
            # sync with unintended settings (or crash later); fail fast instead.
            parser.error(f"config file not found: {args.config}")
        return explicit_config
    discovered_path = discover_config_file(pathing.user_config_dir())
    if discovered_path is None and required:
        legacy_path = discover_json_migration_input()
        if legacy_path is not None:
            parser.error(legacy_json_migration_message(legacy_path))
        parser.error("no global config.toml found; pass --config to choose a file")
    return discovered_path


def load_config_path(
    config_path: Path | None,
    parser: ArgumentParser,
) -> ConfigDict:
    if config_path is None:
        return {}
    try:
        return read_config_file(config_path)
    except ConfigValidationError as error:
        parser.error(str(error))
    except OSError as error:
        parser.error(f"could not read config file {config_path}: {error}")
    except ValueError as error:
        parser.error(f"could not parse config file {config_path}: {error}")


def load_config(args: Namespace, parser: ArgumentParser) -> ConfigDict:
    """Read the explicit config or the global config, if present."""
    return load_config_path(selected_config_path(args, parser), parser)


def config_or_error(
    config: Mapping[str, Any],
    parser: ArgumentParser,
    *,
    config_path: Path | None = None,
) -> Config:
    try:
        return Config.from_dict(config, config_path=config_path)
    except ConfigValidationError as error:
        parser.error(str(error))


def validate_migration_paths(
    input_path: Path, output_path: Path, force: bool = False
) -> None:
    if not input_path.is_file():
        msg = f"config file not found: {input_path}"
        raise FileNotFoundError(msg)
    validate_migration_destination(input_path, output_path, "TOML output")
    if output_path.exists() and not force:
        msg = f"TOML config already exists: {output_path}; use --force to overwrite"
        raise FileExistsError(msg)


def validate_migration_destination(
    input_path: Path, destination: str | Path | None, label: str
) -> None:
    if pathing.path_identity(input_path) == pathing.path_identity(destination):
        raise ValueError(
            f"{label} must not resolve to the same path as migration input: "
            f"{input_path}"
        )


def migrate_config_command(args: Namespace, parser: ArgumentParser) -> None:
    input_path = (
        pathing.absolute_path(Path(args.input))
        if args.input
        else discover_json_migration_input()
    )
    if input_path is None:
        parser.error(
            "no legacy config.json found; pass --input to choose a file explicitly"
        )

    output_path = (
        pathing.absolute_path(Path(args.output))
        if args.output
        else input_path.with_suffix(".toml")
    )
    try:
        validate_migration_paths(input_path, output_path, args.force)
        raw, unresolved = read_legacy_config_file(input_path)
        config_values = resolve_relative_path_options(unresolved, input_path.parent)
        config_values["auth.login.provider"] = "prompt"
        config_values.pop("auth.login.keyring_store_totp_secret", None)
        config_values["auth.tokens.store"] = args.token_store
        if args.token_store == "env-file":
            if not args.token_env_file:
                parser.error("--token-env-file is required with --token-store env-file")
            config_values["auth.tokens.env_file"] = str(
                pathing.absolute_path(Path(args.token_env_file))
            )
        else:
            config_values.pop("auth.tokens.env_file", None)
        config = config_or_error(config_values, parser, config_path=output_path)
        validate_migration_destination(
            input_path, config.cookie_file, "paths.cookie_file"
        )
        if config.token_store == "env-file":
            validate_migration_destination(
                input_path, config.token_env_file, "auth.tokens.env_file"
            )
        ctx = SyncContext(config=config)
        password = raw.get("password")
        totp_secret = raw.get("totpsecret")
        if raw.get("use_secret_service") and config.user:
            provider = KeyringProvider(load_keyring_backend())
            password = provider.get_secret(config.user)
            if config.totp_serial:
                totp_secret = provider.get_secret(config.totp_serial)
        ctx.auth.password = password if isinstance(password, str) else None
        ctx.auth.totp_secret = totp_secret if isinstance(totp_secret, str) else None
        store = token_store_from_config(config, None)
        require_store_available(store, parser)
        print(
            "Logging in once to obtain Moodle tokens for the migrated configuration..."
        )
        rwth.login(ctx, logger, reuse_cached_session=False)
        tokens = acquire_validated_moodle_tokens(ctx, parser)
        with token_store_transaction(store, tokens):
            write_private_text(
                output_path,
                migrated_config_text(config_values, config),
                "migrated config",
            )
    except (
        ConfigValidationError,
        FileNotFoundError,
        FileExistsError,
        OSError,
        ProviderSecretError,
        ValueError,
    ) as error:
        parser.error(str(error))
    print(f"Wrote TOML config to {output_path}")
    print(f"Stored Moodle tokens in {store.description}")
    if raw.get("password") or raw.get("totpsecret"):
        print(
            f"The source JSON was left unchanged and still contains secrets: {input_path}"
        )
    else:
        print(f"The source JSON was left unchanged: {input_path}")
    print(
        "Review the migrated TOML and source JSON, then delete the source JSON "
        "after confirming the migration."
    )


def path_config_command() -> None:
    config_dir = pathing.user_config_dir()
    discovered_path = discover_config_file(config_dir)
    print(f"Global config directory: {config_dir}")
    print(f"Default TOML config: {global_config_path()}")
    print(f"Discovered config: {discovered_path if discovered_path else '<none>'}")


def check_config_command(args: Namespace, parser: ArgumentParser) -> None:
    config_path = selected_config_path(args, parser, required=True)
    assert config_path is not None
    try:
        read_config_file(config_path)
    except ConfigValidationError as error:
        print(f"Config is invalid: {config_path}", file=sys.stderr)
        for detail in error.errors:
            print(f"- {detail}", file=sys.stderr)
        raise SystemExit(1) from error
    except (OSError, ValueError) as error:
        print(f"Could not read config: {config_path}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(f"Config is valid: {config_path}")


def run_config_command(args: Namespace, parser: ArgumentParser) -> None:
    if args.config_command == "example":
        print(starter_config_text(), end="")
        return
    if args.config_command == "path":
        path_config_command()
        return
    if args.config_command == "migrate":
        migrate_config_command(args, parser)
        return
    if args.config_command == "check":
        check_config_command(args, parser)
        return
    parser.error(f"unknown config command: {args.config_command}")


def login_auth_command(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any,
) -> None:
    ctx = configured_auth_context(args, parser, keyring_backend)
    ctx.config.dry_run = False
    try:
        store = token_store_from_config(ctx.config, keyring_backend)
    except ProviderSecretError as error:
        parser.error(str(error))
    require_store_available(store, parser)
    print("Logging in to obtain the current Moodle tokens...")
    rwth.login(ctx, logger, reuse_cached_session=False)
    tokens = acquire_validated_moodle_tokens(ctx, parser)
    try:
        store_tokens_verified(store, tokens)
    except ProviderSecretError as error:
        parser.error(str(error))
    print(f"Stored Moodle tokens in {store.description}")
    print(f"Browser session cached in {ctx.config.cookie_file}")


def prompt_text(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return default if value == "" and default is not None else value


def prompt_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{prompt} [{suffix}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


def setup_sync_directory_value(value: str) -> str:
    return str(pathing.absolute_path(Path(value), Path.cwd()))


def prompt_setup_config(
    parser: ArgumentParser,
) -> tuple[ConfigDict, str, str]:
    username = prompt_text("RWTH SSO username")
    if not username:
        parser.error("RWTH SSO username is required")
    totp_serial = prompt_text("RWTH SSO TOTP serial (for example, TOTP12345678)")
    if not totp_serial:
        parser.error("RWTH SSO TOTP serial is required")
    sync_directory = prompt_text(
        "Directory to sync Moodle files to",
        str(Path.cwd()),
    )
    config: ConfigDict = {
        "auth.user": username,
        "auth.login.totp_serial": totp_serial,
        "auth.login.provider": "prompt",
        "paths.sync_directory": setup_sync_directory_value(sync_directory),
    }
    return config, username, totp_serial


def mutable_toml_table(
    document: MutableMapping[str, Any],
    name: str,
) -> MutableMapping[str, Any]:
    table = document.get(name)
    if table is None:
        table = tomlkit.table()
        document[name] = table
    if not isinstance(table, MutableMapping):
        raise ValueError(f"{name} must be a TOML table")
    return table


def rewrite_token_store_toml(
    config_path: Path, text: str, token_store: str, env_file: str | None
) -> str:
    document = tomlkit.parse(text)
    auth = mutable_toml_table(document, "auth")
    tokens = auth.get("tokens")
    if tokens is None:
        tokens = tomlkit.table()
        auth["tokens"] = tokens
    if not isinstance(tokens, MutableMapping):
        raise ValueError("auth.tokens must be a TOML table")
    tokens["store"] = token_store
    if env_file is None:
        tokens.pop("env_file", None)
    else:
        tokens["env_file"] = env_file
    updated_text = tomlkit.dumps(document)
    updated = resolve_relative_path_options(
        tomllib.loads(updated_text),
        config_path.parent,
    )
    errors = config_validation_errors(updated, config_path=config_path)
    if errors:
        raise ConfigValidationError(config_path, errors)
    return updated_text


def token_store_from_config(
    config: Config,
    keyring_backend: Any,
) -> MoodleTokenStore:
    if not config.user:
        raise ProviderSecretError("auth.user is required")
    if config.token_store == "keyring":
        backend = load_keyring_backend() if keyring_backend is None else keyring_backend
        return KeyringTokenStore(KeyringProvider(backend), config.user)
    if config.token_store == "env-file" and config.token_env_file:
        return EnvFileTokenStore(Path(config.token_env_file), config.user)
    raise ProviderSecretError("auth.tokens.env_file is required")


def require_store_available(store: MoodleTokenStore, parser: ArgumentParser) -> None:
    availability = store.check_available()
    if not availability.available:
        parser.error(f"Moodle token store is unavailable ({availability.reason})")


def acquire_validated_moodle_tokens(
    ctx: SyncContext,
    parser: ArgumentParser,
) -> MoodleTokens:
    assert ctx.auth.user is not None
    try:
        tokens = moodle_api.acquire_mobile_tokens(ctx.session, ctx.auth.user)
    except moodle_api.MobileLaunchError as error:
        parser.error(str(error))
    result = moodle_api.validate_mobile_tokens(tokens)
    if result.kind is not moodle_api.TokenValidationKind.VALID:
        detail = f": {result.detail}" if result.detail else ""
        parser.error(f"could not validate Moodle tokens{detail}")
    return tokens


def prompt_setup_token_store(
    config: ConfigDict,
    username: str,
    keyring_backend: Any,
) -> None:
    backend = load_keyring_backend() if keyring_backend is None else keyring_backend
    keyring_store = KeyringTokenStore(KeyringProvider(backend), username)
    availability = keyring_store.check_available()
    use_keyring = availability.available and prompt_yes_no(
        "Store Moodle tokens in the system keyring (recommended)", default=True
    )
    if use_keyring:
        config["auth.tokens.store"] = "keyring"
        return
    if not availability.available:
        print(
            f"System keyring storage is unavailable ({availability.reason}).",
            file=sys.stderr,
        )
    default_path = pathing.user_config_dir() / "moodle-tokens.env"
    env_file = prompt_text(
        "File for securely storing Moodle tokens",
        str(default_path),
    )
    if not env_file:
        raise ProviderSecretError("a secure Moodle token store is required")
    config["auth.tokens.store"] = "env-file"
    config["auth.tokens.env_file"] = str(pathing.absolute_path(Path(env_file)))


def password_manager_display_name(provider_name: str) -> str:
    return {
        "1password": "1Password",
        "bitwarden": "Bitwarden",
    }.get(provider_name, provider_name)


def normalize_secret_reference(reference: str) -> str:
    if len(reference) >= 2 and reference[0] == reference[-1] == '"':
        return reference[1:-1]
    return reference


def prompt_setup_password_manager(
    config: ConfigDict,
    parser: ArgumentParser,
) -> None:
    for provider_name in detect_password_manager_clis():
        display_name = password_manager_display_name(provider_name)
        if not prompt_yes_no(f"Use {display_name} for RWTH sign-ins"):
            continue
        if provider_name == "1password":
            password_example = "op://Private/RWTH/password"
            otp_example = "op://Private/RWTH/one-time password?attribute=otp"
        else:
            password_example = otp_example = "rwth/sso"
        password_ref = normalize_secret_reference(
            prompt_text(
                f"{display_name} password reference (for example, {password_example})"
            )
        )
        if not password_ref:
            parser.error(f"{display_name} password reference is required")
        otp_ref = normalize_secret_reference(
            prompt_text(
                f"{display_name} TOTP reference (for example, {otp_example}; "
                "optional, blank means prompt for codes)"
            )
        )
        config["auth.login.provider"] = provider_name
        config["auth.login.password"] = password_ref
        if otp_ref:
            config["auth.login.otp"] = otp_ref
        return


def setup_command(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any,
) -> None:
    target_path = global_config_path()
    existing_path = discover_config_file(pathing.user_config_dir())
    if existing_path is not None:
        parser.error(
            f"syncMyMoodle is already configured at {existing_path}; "
            "edit that file manually to change settings. Run "
            "`syncmymoodle config path` to show configuration locations or "
            "`syncmymoodle auth status` to inspect authentication."
        )
    legacy_path = discover_json_migration_input()
    if legacy_path is not None:
        parser.error(legacy_json_migration_message(legacy_path))

    config, username, _ = prompt_setup_config(parser)
    prompt_setup_password_manager(config, parser)
    try:
        prompt_setup_token_store(config, username, keyring_backend)
    except ProviderSecretError as error:
        parser.error(str(error))
    text = starter_config_text(config)
    ctx = SyncContext(
        config=config_or_error(tomllib.loads(text), parser, config_path=target_path)
    )
    configure_secret_resolvers(ctx, args, keyring_backend)
    store = token_store_from_config(ctx.config, keyring_backend)
    require_store_available(store, parser)

    print("Logging in once to obtain Moodle tokens...")
    rwth.login(
        ctx,
        logger,
        reuse_cached_session=False,
        persist_session=False,
    )
    tokens = acquire_validated_moodle_tokens(ctx, parser)
    limited_opencast = tokens.private_token is None
    if limited_opencast and not prompt_yes_no(
        "Moodle did not provide the browser login token required for embedded "
        "Opencast downloads. Finish setup with limited Opencast support"
    ):
        parser.error(
            "setup cancelled before saving the configuration or Moodle tokens. "
            "To repair the shared token, rerun setup, finish with limited Opencast "
            "support, then run `syncmymoodle auth reset-token`."
        )

    try:
        with token_store_transaction(store, tokens):
            write_private_text(target_path, text, "global config")
    except (OSError, ProviderSecretError, ValueError) as error:
        parser.error(f"could not write global config {target_path}: {error}")
    print(f"Setup complete. Wrote global config to {target_path}")
    print(f"Stored Moodle tokens in {store.description}")
    print(
        "Normal syncs use the stored Moodle tokens and will not ask for your "
        "RWTH password or TOTP code."
    )
    if limited_opencast:
        print(
            "Embedded Opencast downloads may stop working after the cached browser "
            "session expires. Run `syncmymoodle auth reset-token` to create a "
            "complete token pair."
        )
    print("Run `syncmymoodle` to start syncing.")


@dataclass(frozen=True)
class LoadedAuthConfig:
    path: Path
    source: LoadedConfig
    config: Config


def load_auth_config(
    args: Namespace,
    parser: ArgumentParser,
) -> LoadedAuthConfig:
    config_path = selected_config_path(args, parser, required=True)
    assert config_path is not None
    try:
        loaded = read_config_file_unresolved(config_path)
        resolved = resolve_relative_path_options(loaded.values, config_path.parent)
        config = Config.from_dict(resolved, config_path=config_path)
    except ConfigValidationError as error:
        parser.error(str(error))
    except OSError as error:
        parser.error(f"could not read config file {config_path}: {error}")
    except ValueError as error:
        parser.error(f"could not parse config file {config_path}: {error}")
    return LoadedAuthConfig(config_path, loaded, config)


def configured_auth_context(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any,
) -> SyncContext:
    ctx = SyncContext(config=load_auth_config(args, parser).config)
    configure_secret_resolvers(
        ctx,
        args,
        keyring_backend,
        resolve_otp=not getattr(args, "totp_manual", False),
    )
    return ctx


def migrate_auth_command(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any,
) -> None:
    auth_config = load_auth_config(args, parser)
    config_path = auth_config.path
    loaded = auth_config.source
    config = auth_config.config
    try:
        source = token_store_from_config(config, keyring_backend)
        require_store_available(source, parser)
        tokens = source.load()
        if tokens is None:
            ctx = SyncContext(config=config)
            configure_secret_resolvers(ctx, args, keyring_backend)
            print("No stored Moodle tokens found; logging in once before migration...")
            rwth.login(ctx, logger, reuse_cached_session=False)
            tokens = acquire_validated_moodle_tokens(ctx, parser)
        else:
            validation = moodle_api.validate_mobile_tokens(tokens)
            if validation.kind is not moodle_api.TokenValidationKind.VALID:
                detail = f": {validation.detail}" if validation.detail else ""
                parser.error(f"stored Moodle tokens are not valid{detail}")
        env_file: str | None = None
        if args.to == "keyring":
            assert config.user is not None
            backend = (
                load_keyring_backend() if keyring_backend is None else keyring_backend
            )
            destination: MoodleTokenStore = KeyringTokenStore(
                KeyringProvider(backend), config.user
            )
        else:
            if not args.env_file:
                parser.error("--env-file is required with --to env-file")
            assert config.user is not None
            env_file = str(pathing.absolute_path(Path(args.env_file)))
            destination = EnvFileTokenStore(Path(env_file), config.user)
        require_store_available(destination, parser)
        updated_text = rewrite_token_store_toml(
            config_path, loaded.text, args.to, env_file
        )
        with token_store_transaction(destination, tokens):
            write_private_text(config_path, updated_text, "authentication config")
    except (ConfigValidationError, OSError, ProviderSecretError, ValueError) as error:
        parser.error(str(error))

    print(
        f"Copied Moodle tokens to {destination.description} and updated {config_path}"
    )
    if source.description != destination.description:
        print(f"The previous {source.description} was left untouched.")


def sign_in_method_description(config: Config, keyring_backend: Any) -> str:
    source = config.auth_source
    if isinstance(source, KeyringAuthSource):
        keyring_provider = KeyringProvider(keyring_backend)
        return (
            "system keyring "
            f"({provider_availability_text(keyring_provider.check_available())})"
        )
    if isinstance(source, EnvFileAuthSource):
        state = "available" if source.path.is_file() else "missing"
        return f"protected environment file {source.path} ({state})"
    if isinstance(source, ExternalAuthSource):
        try:
            availability = build_external_secret_provider(
                source.provider
            ).check_available()
        except (ProviderSecretError, ValueError) as error:
            availability = ProviderAvailability(False, str(error))
        return (
            f"{password_manager_display_name(source.provider)} CLI "
            f"({provider_availability_text(availability)})"
        )
    if isinstance(source, CommandAuthSource):
        command_provider = CommandSecretProvider(
            source.password_command,
            source.otp_command,
        )
        availability = command_provider.check_available()
        if availability.available and source.otp_command:
            availability = command_provider.check_otp_available()
        return f"configured command ({provider_availability_text(availability)})"
    if isinstance(source, PromptAuthSource):
        return "interactive prompt when needed"
    raise AssertionError(f"unknown authentication source: {source!r}")


def provider_availability_text(availability: ProviderAvailability) -> str:
    if availability.available:
        return "available"
    return f"unavailable: {availability.reason or 'unknown reason'}"


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def report_stored_moodle_tokens(
    config: Config,
    backend: Any,
) -> bool:
    try:
        store = token_store_from_config(config, backend)
        availability = store.check_available()
        state = (
            "available"
            if availability.available
            else f"unavailable: {availability.reason}"
        )
        print(f"Token storage: {store.description} ({state})")
        if not availability.available:
            print("Moodle tokens: unavailable")
            return True
        tokens = store.load()
    except ProviderSecretError as error:
        print(f"Moodle tokens: unavailable ({error})")
        return True
    if tokens is None:
        print("Moodle tokens: missing")
        print("Run `syncmymoodle auth login` to create them.")
        return True
    return report_moodle_tokens(config, tokens)


def report_moodle_tokens(config: Config, tokens: MoodleTokens) -> bool:
    token_status = moodle_api.validate_mobile_tokens(tokens)
    failed = False
    if token_status.kind is moodle_api.TokenValidationKind.VALID:
        print("Moodle API token: valid")
        print("API token expiry: not reported by Moodle")
    elif token_status.kind is moodle_api.TokenValidationKind.INVALID:
        print(f"Moodle API token: invalid ({token_status.detail})")
        print("Run `syncmymoodle auth login` to replace it.")
        failed = True
    else:
        print(f"Moodle API token: unknown ({token_status.detail})")
        failed = True

    if tokens.private_token:
        print(
            "Browser login token: present "
            "(not tested because Moodle limits how often it can be used)"
        )
    else:
        print("Browser login token: missing")
        if config.link_source_enabled("opencast"):
            print(
                "Embedded Opencast needs a browser login token; run "
                "`syncmymoodle auth reset-token`."
            )
            failed = True
    return failed


def report_cached_session(cookie_file: str) -> None:
    session_status = rwth.cached_session_status(Path(cookie_file))
    if session_status.kind is rwth.SessionStatusKind.VALID:
        assert session_status.remaining_seconds is not None
        print("Cached browser session: valid")
        print(f"Remaining: {format_duration(session_status.remaining_seconds)}")
    elif session_status.kind is rwth.SessionStatusKind.EXPIRED:
        print("Cached browser session: expired")
    elif session_status.kind is rwth.SessionStatusKind.MISSING:
        print("Cached browser session: missing")
    else:
        detail = f" ({session_status.detail})" if session_status.detail else ""
        print(f"Cached browser session: unknown{detail}")


def auth_status_command(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any,
) -> None:
    auth_config = load_auth_config(args, parser)
    config_path = auth_config.path
    config = auth_config.config
    backend = load_keyring_backend() if keyring_backend is None else keyring_backend
    print(f"Configuration: {config_path}")
    account = config.user or "<missing>"
    print(f"Account: {account} @ {MOODLE_NETLOC}")
    print(f"RWTH sign-in method: {sign_in_method_description(config, backend)}")
    failed = report_stored_moodle_tokens(config, backend)
    report_cached_session(config.cookie_file)
    if failed:
        raise SystemExit(1)


def forget_auth_command(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any,
) -> None:
    config = load_auth_config(args, parser).config
    print("This removes authentication data stored only on this installation.")
    print(
        "The shared Moodle API token, configuration, and RWTH sign-in secrets "
        "will remain unchanged."
    )
    if not isinstance(config.auth_source, PromptAuthSource):
        print(
            "The configured RWTH sign-in method remains available, so a later sync "
            "can sign in and store local Moodle tokens again."
        )
    if not prompt_yes_no("Forget local Moodle tokens and cached browser session"):
        print("Local authentication data was left unchanged.")
        return

    errors: list[str] = []
    try:
        store = token_store_from_config(config, keyring_backend)
        store.delete()
        print(f"Removed Moodle tokens from {store.description} (if present).")
    except ProviderSecretError as error:
        errors.append(f"Moodle tokens: {error}")

    cookie_file = Path(config.cookie_file)
    try:
        cookie_file.unlink()
    except FileNotFoundError:
        print(f"Cached browser session was already absent ({cookie_file}).")
    except OSError as error:
        errors.append(f"cached browser session {cookie_file}: {error}")
    else:
        print(f"Removed cached browser session ({cookie_file}).")

    if errors:
        parser.error(
            "could not forget all local authentication data: " + "; ".join(errors)
        )
    print(
        "Local authentication data forgotten; the shared Moodle API token "
        "was not reset."
    )


def reset_token_auth_command(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any,
) -> None:
    ctx = configured_auth_context(args, parser, keyring_backend)
    print("This resets the shared Moodle API token.")
    print(
        "The Moodle app and every other syncMyMoodle installation using it "
        "will need to authenticate again. Other Moodle service tokens are unaffected."
    )
    if not prompt_yes_no("Reset the shared Moodle API token"):
        print("Token reset cancelled.")
        return
    ctx.config.dry_run = False
    try:
        store = token_store_from_config(ctx.config, keyring_backend)
    except ProviderSecretError as error:
        parser.error(str(error))
    require_store_available(store, parser)
    print("Logging in before the explicit token reset...")
    rwth.login(ctx, logger, reuse_cached_session=False)
    if ctx.session_key is None:
        parser.error("logged-in Moodle session did not provide a session key")
    try:
        moodle_api.reset_mobile_token(ctx.require_session(), ctx.session_key)
    except moodle_api.MobileTokenResetError as error:
        parser.error(str(error))
    tokens = acquire_validated_moodle_tokens(ctx, parser)
    if tokens.private_token is None:
        parser.error(
            "Moodle reset the API token but did not return a browser login token; "
            "the previous stored token is now invalid"
        )
    try:
        store_tokens_verified(store, tokens)
    except ProviderSecretError as error:
        parser.error(
            f"token was reset but the replacement could not be stored: {error}"
        )
    print(f"Reset and stored the replacement Moodle tokens in {store.description}")


def run_auth_command(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any,
) -> None:
    if args.auth_command == "login":
        login_auth_command(args, parser, keyring_backend)
        return
    if args.auth_command == "migrate":
        migrate_auth_command(args, parser, keyring_backend)
        return
    if args.auth_command == "status":
        auth_status_command(args, parser, keyring_backend)
        return
    if args.auth_command == "forget":
        forget_auth_command(args, parser, keyring_backend)
        return
    if args.auth_command == "reset-token":
        reset_token_auth_command(args, parser, keyring_backend)
        return
    parser.error(f"unknown auth command: {args.auth_command}")


def count_phrase(count: int, singular: str, plural: str) -> str:
    noun = singular if count == 1 else plural
    return f"{count} {noun}"


def cleanup_root_from_args(args: Namespace, parser: ArgumentParser) -> Path:
    if args.path:
        root = pathing.absolute_path(Path(args.path))
    else:
        file_config = load_config(args, parser)
        has_configured_root = bool(file_config.get("paths.sync_directory"))
        if args.apply and not has_configured_root:
            parser.error(
                "clean --apply requires --path or a configured paths.sync_directory"
            )
        root = Path(config_or_error(file_config, parser).sync_directory)
    root = root.expanduser()
    if not root.is_dir():
        parser.error(f"{root} is not a directory")
    return root


def clean_conflicts_command(args: Namespace, parser: ArgumentParser) -> None:
    root = cleanup_root_from_args(args, parser)
    conflicts = cleanup.iter_conflicts(root)
    plan = cleanup.conflict_cleanup_plan(conflicts)
    action = "Deleting" if args.apply else "Would delete"
    for path in plan.remove:
        print(f"{action}: {path}")
    if args.apply:
        cleanup.delete_paths(plan.remove)

    print(
        f"Scanned {count_phrase(len(conflicts), 'syncconflict file', 'syncconflict files')}; "
        f"{count_phrase(len(plan.remove), 'file', 'files')} "
        f"{'deleted' if args.apply else 'would be deleted'}; "
        f"{count_phrase(len(plan.keep), 'unique differing conflict file', 'unique differing conflict files')} kept."
    )
    if not args.apply:
        print("Dry run only. Re-run with --apply to delete these files.")


def clean_caches_command(args: Namespace, parser: ArgumentParser) -> None:
    root = cleanup_root_from_args(args, parser)
    cache_paths = cleanup.iter_course_caches(root)
    print(
        "This resets syncMyMoodle metadata caches. It is usually only useful "
        "when recovering from broken or stale cache metadata."
    )
    action = "Deleting" if args.apply else "Would delete"
    for path in cache_paths:
        print(f"{action}: {path}")
    if args.apply:
        cleanup.delete_paths(cache_paths)

    print(
        f"Scanned {root}; "
        f"{count_phrase(len(cache_paths), 'cache file', 'cache files')} "
        f"{'deleted' if args.apply else 'would be deleted'}."
    )
    if not args.apply:
        print(
            "Dry run only. Re-run with --apply to delete these files. "
            "Do this only when you intentionally want the next sync to rebuild "
            "course metadata caches."
        )


def run_clean_command(args: Namespace, parser: ArgumentParser) -> None:
    if args.clean_command == "conflicts":
        clean_conflicts_command(args, parser)
        return
    if args.clean_command == "caches":
        clean_caches_command(args, parser)
        return
    parser.error(f"unknown clean command: {args.clean_command}")


def apply_cli_overrides(
    config: ConfigDict,
    args: Namespace,
    path_base: Path | None = None,
) -> None:
    path_base = Path.cwd() if path_base is None else path_base
    for option in CONFIG_OPTIONS:
        cli = option.cli
        if cli is None:
            continue
        value = getattr(args, cli.dest, None)
        if cli.value_kind == "flag":
            if value is not None:
                config[option.canonical_key] = value
        elif value is not None:
            if cli.value_kind == "csv":
                config[option.canonical_key] = [] if value == "" else value.split(",")
            else:
                config[option.canonical_key] = (
                    str(pathing.absolute_path(Path(value), path_base))
                    if option.resolve_relative_path and value
                    else value
                )
                if option.canonical_key == "auth.login.env_file" and value:
                    config["auth.login.provider"] = "env-file"
                    for incompatible_key in (
                        "auth.login.password",
                        "auth.login.otp",
                        "auth.login.password_command",
                        "auth.login.otp_command",
                    ):
                        config.pop(incompatible_key, None)
                    if getattr(args, "keyring_store_totp_secret", None) is None:
                        config.pop("auth.login.keyring_store_totp_secret", None)


def get_or_prompt_stored_secret(
    get_secret: Callable[[str], str | None],
    store_secret: Callable[[str, str], None],
    reference: str,
    prompt: str,
) -> str:
    secret = get_secret(reference)
    if secret:
        return secret

    secret = getpass.getpass(prompt)
    if not secret:
        raise ProviderSecretError(f"{prompt.rstrip(':')} is required")
    store_secret(reference, secret)
    return secret


def resolve_keyring_credentials(
    auth: AuthState,
    store_totp_secret: bool,
    keyring_backend: Any,
) -> None:
    if keyring_backend is None:
        keyring_backend = load_keyring_backend()
    provider = KeyringProvider(keyring_backend)
    try:
        availability = provider.check_available()
        if not availability.available:
            logger.critical(
                "auth.login.provider is 'keyring', but no usable system keyring is "
                "available (%s). Install or unlock a supported OS keyring "
                "backend, or change auth.login.provider.",
                availability.reason,
            )
            sys.exit(1)

        if not auth.password:
            assert auth.user is not None
            auth.password = get_or_prompt_stored_secret(
                provider.get_secret,
                provider.store_secret,
                auth.user,
                "Password:",
            )
        if store_totp_secret and not auth.totp_secret:
            assert auth.totp_serial is not None
            auth.totp_secret = get_or_prompt_stored_secret(
                provider.get_secret,
                provider.store_secret,
                auth.totp_serial,
                "TOTP-Secret:",
            )
    except ProviderSecretError as error:
        logger.critical("%s", error)
        sys.exit(1)


def resolve_env_file_credentials(
    auth: AuthState,
    path: Path,
    load_totp_secret: bool,
) -> None:
    try:
        credentials = EnvFileProvider(path).load_credentials()
    except ProviderSecretError as error:
        logger.critical("auth.login.env_file could not be read: %s", error)
        sys.exit(1)
    if not auth.password:
        auth.password = credentials.password
    if load_totp_secret and not auth.totp_secret:
        auth.totp_secret = credentials.totp_secret
    if not auth.password:
        logger.critical("auth.login.env_file does not define SYNCMYMOODLE_PASSWORD.")
        sys.exit(1)


def ensure_provider_available(
    check_available: Callable[[], ProviderAvailability],
    unavailable_message: str,
) -> None:
    try:
        availability = check_available()
    except ProviderSecretError as error:
        logger.critical("%s", error)
        sys.exit(1)
    if not availability.available:
        logger.critical(unavailable_message, availability.reason)
        sys.exit(1)


def resolve_secret_provider_password(
    auth: AuthState,
    provider: SecretProvider,
    ensure_available: Callable[[], None],
    reference: str,
    setting_name: str,
) -> None:
    try:
        ensure_available()
        if not auth.password:
            auth.password = provider.get_password(reference)
    except ProviderSecretError as error:
        logger.critical("%s", error)
        sys.exit(1)

    if not auth.password:
        logger.critical("%s did not return a password.", setting_name)
        sys.exit(1)


def resolve_secret_provider_otp(
    provider: SecretProvider,
    ensure_available: Callable[[], None],
    reference: str,
    setting_name: str,
) -> str | None:
    try:
        ensure_available()
        otp_code = provider.get_otp_code(reference)
    except ProviderSecretError as error:
        logger.critical("%s", error)
        sys.exit(1)
    if not otp_code:
        logger.critical("%s did not return an OTP code.", setting_name)
        sys.exit(1)
    return str(otp_code)


def configure_read_provider_resolvers(
    auth: AuthState,
    provider: SecretProvider,
    password_reference: str,
    otp_reference: str | None,
    ensure_password_available: Callable[[], None],
    ensure_otp_available: Callable[[], None],
    password_setting: str,
    otp_setting: str,
) -> None:
    if not auth.password:
        auth.credential_resolver = lambda: resolve_secret_provider_password(
            auth,
            provider,
            ensure_password_available,
            password_reference,
            password_setting,
        )
    if otp_reference is not None and not auth.totp_secret:
        auth.otp_code_resolver = lambda: resolve_secret_provider_otp(
            provider,
            ensure_otp_available,
            otp_reference,
            otp_setting,
        )


def provider_availability_guard(
    check_available: Callable[[], ProviderAvailability],
    unavailable_message: str,
) -> Callable[[], None]:
    checked = False

    def guard() -> None:
        nonlocal checked
        if checked:
            return
        ensure_provider_available(check_available, unavailable_message)
        checked = True

    return guard


def configure_keyring_resolver(
    auth: AuthState,
    source: KeyringAuthSource,
    keyring_backend: Any,
    resolve_otp: bool,
) -> None:
    store_totp_secret = source.store_totp_secret and resolve_otp
    if not auth.password or (store_totp_secret and not auth.totp_secret):
        auth.credential_resolver = lambda: resolve_keyring_credentials(
            auth,
            store_totp_secret,
            keyring_backend,
        )


def configure_external_secret_provider_resolvers(
    auth: AuthState,
    source: ExternalAuthSource,
    resolve_otp: bool = True,
) -> None:
    try:
        provider = build_external_secret_provider(source.provider)
    except ValueError as error:
        logger.critical("%s", error)
        sys.exit(1)

    ensure_available = provider_availability_guard(
        provider.check_available,
        f"auth.login.provider {source.provider!r} is configured, but unavailable (%s).",
    )
    configure_read_provider_resolvers(
        auth,
        provider,
        source.password_reference,
        source.otp_reference if resolve_otp else None,
        ensure_available,
        ensure_available,
        "auth.login.password",
        "auth.login.otp",
    )


def configure_command_secret_provider_resolvers(
    auth: AuthState,
    source: CommandAuthSource,
    args: Namespace,
    resolve_otp: bool = True,
) -> None:
    if args.config:
        logger.critical(
            "auth.login.provider = 'command' is only allowed from the "
            "default global config, not from --config."
        )
        sys.exit(1)
    provider = CommandSecretProvider(
        source.password_command,
        source.otp_command,
    )
    ensure_password_available = provider_availability_guard(
        provider.check_available,
        "auth.login.provider 'command' is configured, but unavailable (%s).",
    )
    ensure_otp_available = provider_availability_guard(
        provider.check_otp_available,
        "auth.login.otp_command is configured, but unavailable (%s).",
    )
    configure_read_provider_resolvers(
        auth,
        provider,
        "",
        "" if source.otp_command and resolve_otp else None,
        ensure_password_available,
        ensure_otp_available,
        "auth.login.password_command",
        "auth.login.otp_command",
    )


def configure_secret_resolvers(
    ctx: SyncContext,
    args: Namespace,
    keyring_backend: Any,
    *,
    resolve_otp: bool = True,
) -> None:
    source = ctx.config.auth_source
    if isinstance(source, KeyringAuthSource):
        configure_keyring_resolver(
            ctx.auth,
            source,
            keyring_backend,
            resolve_otp,
        )
    elif isinstance(source, CommandAuthSource):
        configure_command_secret_provider_resolvers(
            ctx.auth,
            source,
            args,
            resolve_otp=resolve_otp,
        )
    elif isinstance(source, ExternalAuthSource):
        configure_external_secret_provider_resolvers(
            ctx.auth,
            source,
            resolve_otp=resolve_otp,
        )
    elif isinstance(source, EnvFileAuthSource):
        needs_totp_secret = resolve_otp and not ctx.auth.totp_secret
        if not ctx.auth.password or needs_totp_secret:
            ctx.auth.credential_resolver = lambda: resolve_env_file_credentials(
                ctx.auth,
                source.path,
                load_totp_secret=resolve_otp,
            )


def config_from_args(
    args: Namespace,
    parser: ArgumentParser,
) -> Config:
    config_path = selected_config_path(args, parser)
    file_config = load_config_path(config_path, parser)
    merged = dict(file_config)
    apply_cli_overrides(merged, args)
    return config_or_error(merged, parser, config_path=config_path)


def context_from_args(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any = None,
) -> SyncContext:
    ctx = SyncContext(config=config_from_args(args, parser))
    configure_secret_resolvers(ctx, args, keyring_backend)
    return ctx


def has_cli_config_overrides(args: Namespace) -> bool:
    for option in CONFIG_OPTIONS:
        if option.cli is None:
            continue
        value = getattr(args, option.cli.dest, None)
        if value is not None:
            return True
    return False


def validate_command_option_scope(args: Namespace, parser: ArgumentParser) -> None:
    if args.command is None:
        return
    sync_options = [
        f"--{option.cli.arg_name}"
        for option in CONFIG_OPTIONS
        if option.cli is not None and getattr(args, option.cli.dest, None) is not None
    ]
    if args.show_filtered:
        sync_options.append("--show-filtered")
    if sync_options:
        parser.error(
            f"sync options cannot be used with `{args.command}`: "
            + ", ".join(sync_options)
        )
    if not args.config:
        return
    if args.command == "setup":
        parser.error("setup writes the global config; do not pass --config")
    if args.command == "config" and args.config_command != "check":
        parser.error("--config is only supported with `config check`")


def configure_browser_session_resolver(ctx: SyncContext) -> None:
    def resolve() -> None:
        # A failed request must not repeatedly consume the shared six-minute
        # auto-login rate limit during the same run.
        ctx.browser_session_resolver = None
        account = ctx.moodle_account
        if account is None:
            raise BrowserSessionUnavailable(
                "Moodle tokens are unavailable for browser login"
            )
        cookie_file = Path(ctx.config.cookie_file)
        status = rwth.cached_session_status(cookie_file)
        if status.kind is rwth.SessionStatusKind.VALID:
            cached = rwth.load_cached_session(cookie_file)
            if cached is not None:
                cached_session, cached_session_key = cached
                try:
                    cached_user_id = moodle_api.browser_session_user_id(cached_session)
                except moodle_api.BrowserSessionIdentityError as error:
                    logger.warning(
                        "Could not verify cached Moodle browser session: %s", error
                    )
                else:
                    if cached_user_id == account.user_id:
                        ctx.browser_session = cached_session
                        ctx.browser_session_key = cached_session_key
                        return
                    logger.warning(
                        "Ignoring cached Moodle browser session for another account"
                    )
        try:
            session, session_key = moodle_api.create_browser_session(account.tokens)
        except moodle_api.BrowserBootstrapError as error:
            raise BrowserSessionUnavailable(str(error)) from error
        ctx.browser_session = session
        ctx.browser_session_key = session_key
        if not ctx.config.dry_run:
            save_session(cookie_file, session.cookies, session_key)

    ctx.browser_session_resolver = resolve


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_command_option_scope(args, parser)
    logging.basicConfig(level=args.loglevel)
    if args.command == "setup":
        setup_command(args, parser, None)
        return
    if args.command == "config":
        run_config_command(args, parser)
        return
    if args.command == "auth":
        run_auth_command(args, parser, None)
        return
    if args.command == "clean":
        run_clean_command(args, parser)
        return
    if (
        not args.config
        and discover_config_file(pathing.user_config_dir()) is None
        and not has_cli_config_overrides(args)
    ):
        legacy_path = discover_json_migration_input()
        if legacy_path is not None:
            parser.error(legacy_json_migration_message(legacy_path))
        parser.error("no global config found; run `syncmymoodle setup` first")
    run(context_from_args(args, parser), show_filtered=args.show_filtered)


def load_stored_moodle_tokens(
    ctx: SyncContext,
) -> tuple[
    MoodleTokenStore,
    MoodleTokens | None,
    moodle_api.TokenValidation | None,
]:
    try:
        store = token_store_from_config(ctx.config, None)
        availability = store.check_available()
        if not availability.available:
            raise ProviderSecretError(
                f"Moodle token store is unavailable ({availability.reason})"
            )
        tokens = store.load()
    except ProviderSecretError as error:
        logger.critical("%s", error)
        raise SystemExit(1) from error

    validation: moodle_api.TokenValidation | None = None
    if tokens is not None:
        validation = moodle_api.validate_mobile_tokens(tokens)
    return store, tokens, validation


def reauthenticate_moodle_tokens(
    ctx: SyncContext,
    store: MoodleTokenStore,
) -> tuple[MoodleTokens, moodle_api.TokenValidation]:
    if isinstance(ctx.config.auth_source, PromptAuthSource):
        logger.critical(
            "Moodle tokens are missing or invalid, and the configured RWTH sign-in "
            "method requires interaction. Run `syncmymoodle auth login`."
        )
        raise SystemExit(1)
    print("Automatically re-authenticating with RWTH SSO...")
    rwth.login(ctx, logger, reuse_cached_session=False)
    assert ctx.auth.user is not None
    try:
        tokens = moodle_api.acquire_mobile_tokens(ctx.session, ctx.auth.user)
    except moodle_api.MobileLaunchError as error:
        logger.critical("%s", error)
        raise SystemExit(1) from error
    validation = moodle_api.validate_mobile_tokens(tokens)
    if validation.kind is not moodle_api.TokenValidationKind.VALID:
        logger.critical(
            "Moodle tokens obtained after re-authentication could not be validated: %s",
            validation.detail,
        )
        raise SystemExit(1)
    try:
        store_tokens_verified(store, tokens)
    except ProviderSecretError as error:
        logger.critical(
            "Could not store Moodle tokens after re-authentication: %s", error
        )
        raise SystemExit(1) from error
    return tokens, validation


def resolve_moodle_tokens_for_run(
    ctx: SyncContext,
) -> tuple[MoodleTokens, moodle_api.TokenValidation]:
    store, tokens, validation = load_stored_moodle_tokens(ctx)
    if tokens is None:
        return reauthenticate_moodle_tokens(ctx, store)
    assert validation is not None
    if validation.kind is moodle_api.TokenValidationKind.INVALID:
        return reauthenticate_moodle_tokens(ctx, store)
    if validation.kind is moodle_api.TokenValidationKind.UNKNOWN:
        logger.critical(
            "Could not validate stored Moodle tokens without risking replacement: %s",
            validation.detail,
        )
        raise SystemExit(1)
    return tokens, validation


def report_filtered_items(ctx: SyncContext, show_details: bool) -> None:
    items = sorted(ctx.filtered_items)
    if not items:
        return
    count = len(items)
    if not show_details:
        noun = "item" if count == 1 else "items"
        print(f"Filtered {count} {noun}; use --show-filtered for details.")
        return

    print(f"Filtered items ({count}):")
    for config_key, group in groupby(items, key=lambda item: item.config_key):
        grouped_items = list(group)
        print(f"  {config_key} ({len(grouped_items)}):")
        for item in grouped_items:
            print(f"    {item.category}: {item.item} - {item.reason}")


def run(ctx: SyncContext, *, show_filtered: bool = False) -> None:
    """Execute a full sync run against an already-configured context."""
    tokens, validation = resolve_moodle_tokens_for_run(ctx)

    assert validation.site_info is not None
    ctx.moodle_account = MoodleAccount(tokens)
    private_access_key = validation.site_info.get("userprivateaccesskey")
    user_private_access_key = (
        private_access_key
        if isinstance(private_access_key, str) and private_access_key
        else None
    )
    ctx.session = moodle_api.create_token_session(
        tokens,
        user_private_access_key,
    )
    configure_browser_session_resolver(ctx)
    print("Syncing file tree...")
    sync.sync(ctx)
    if ctx.config.dry_run:
        print("Dry run: listing files that would be downloaded...")
    else:
        print("Downloading files...")
    downloader.download_all_files(ctx, logger)
    if not ctx.config.dry_run:
        print("Saving root node as cache...")
        course_cache.cache_root_node(ctx, logger)
    report_filtered_items(ctx, show_filtered)


if __name__ == "__main__":
    main()
