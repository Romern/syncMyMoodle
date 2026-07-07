#!/usr/bin/env python3

import getpass
import json
import logging
import os
import sys
import tomllib
from argparse import SUPPRESS, ArgumentParser, Namespace
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import tomli_w

from syncmymoodle import course_cache, downloader, rwth, sync
from syncmymoodle import moodle as moodle_api
from syncmymoodle.config import (
    CONFIG_OPTIONS,
    Config,
    ConfigDict,
    ConfigValidationError,
    canonicalize,
    config_validation_errors,
    convert_legacy_config,
    group_config_for_toml,
)
from syncmymoodle.constants import RWTH_MOODLE_STATUS_URL
from syncmymoodle.context import SyncContext

try:
    import keyring as imported_keyring

    keyring: ModuleType | None = imported_keyring
except ImportError:
    keyring = None

logger = logging.getLogger(__name__)
CONFIG_FILENAMES = ("config.toml", "config.json")


def build_parser(keyring_backend: Any = None) -> ArgumentParser:
    parser = ArgumentParser(
        prog="python3 -m syncmymoodle",
        description=(
            "Synchronization client for RWTH Moodle. All optional arguments "
            "override those in config.toml/config.json."
        ),
    )
    parser.add_argument("--config", default=None, help="set your configuration file")
    for option in CONFIG_OPTIONS:
        cli = option.cli
        if cli is None or (cli.requires_keyring and not keyring_backend):
            continue
        kwargs: dict[str, Any] = {}
        if cli.value_kind == "flag":
            kwargs["action"] = "store_true"
        elif option.choices:
            kwargs["choices"] = option.choices
        parser.add_argument(f"--{cli.arg_name}", help=cli.help, **kwargs)
        if cli.aliases:
            # Deprecated spellings: still accepted, hidden from --help. SUPPRESS
            # keeps an absent alias from clobbering the primary flag's default.
            parser.add_argument(
                *(f"--{alias}" for alias in cli.aliases),
                dest=cli.dest,
                default=SUPPRESS,
                help=SUPPRESS,
                **kwargs,
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

    subparsers = parser.add_subparsers(dest="command")
    config_parser = subparsers.add_parser("config", help="manage configuration files")
    config_subparsers = config_parser.add_subparsers(
        dest="config_command",
        required=True,
    )
    migrate_parser = config_subparsers.add_parser(
        "migrate",
        help="convert a legacy JSON config file to TOML",
    )
    migrate_parser.add_argument(
        "--input",
        default=None,
        help="legacy JSON config to migrate; defaults to local config.json, then the XDG config",
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
    check_parser = config_subparsers.add_parser(
        "check",
        help="validate a configuration file",
    )
    # SUPPRESS keeps a --config given before the subcommand intact: subparser
    # defaults would otherwise overwrite the already-parsed global value.
    check_parser.add_argument(
        "--config",
        default=SUPPRESS,
        help="config file to validate; defaults to discovered config.toml/config.json",
    )
    return parser


def warn_legacy_json_config(path: Path) -> None:
    logger.warning(
        "Loading legacy JSON config %s. TOML config is preferred; run "
        "`syncmymoodle config migrate --input %s` to convert it.",
        path,
        path,
    )


def read_config_file(path: Path, warn_legacy_json: bool = True) -> ConfigDict:
    """Parse, canonicalize and validate a single config file.

    Raises OSError if the file cannot be read, ValueError if it cannot be
    parsed and ConfigValidationError if it fails validation.
    """
    if path.suffix == ".toml":
        with path.open("rb") as fb:
            raw: Any = tomllib.load(fb)
    else:
        if warn_legacy_json:
            warn_legacy_json_config(path)
        with path.open() as f:
            raw = json.load(f)
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"config root must be a table/object, got {type(raw).__name__}"
            )
        raw = convert_legacy_config(raw)
    canonical = canonicalize(raw)
    errors = config_validation_errors(canonical)
    if errors:
        raise ConfigValidationError(path, errors)
    return canonical


def global_config_dir() -> Path:
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return xdg_config_home / "syncmymoodle"


def discover_config_file(directory: Path) -> Path | None:
    for filename in CONFIG_FILENAMES:
        path = directory / filename
        if path.is_file():
            return path
    return None


def discover_json_migration_input() -> Path | None:
    local_config = Path("config.json")
    if local_config.is_file():
        return local_config
    global_config = global_config_dir() / "config.json"
    if global_config.is_file():
        return global_config
    return None


def discover_config_files() -> list[Path]:
    return [
        config_file
        for config_file in (
            discover_config_file(global_config_dir()),
            discover_config_file(Path(".")),
        )
        if config_file is not None
    ]


def load_config(args: Namespace, parser: ArgumentParser) -> ConfigDict:
    """Read and merge all config files into one canonical dict (local wins)."""
    if args.config:
        explicit_config = Path(args.config)
        if not explicit_config.is_file():
            # Silently continuing without the explicitly requested file would
            # sync with unintended settings (or crash later); fail fast instead.
            parser.error(f"config file not found: {args.config}")
        config_paths = [explicit_config]
    else:
        config_paths = discover_config_files()

    config: ConfigDict = {}
    for config_path in config_paths:
        try:
            config.update(read_config_file(config_path))
        except ConfigValidationError as error:
            parser.error(str(error))
        except OSError as error:
            parser.error(f"could not read config file {config_path}: {error}")
        except ValueError as error:
            parser.error(f"could not parse config file {config_path}: {error}")
    return config


def migrate_json_config(
    input_path: Path,
    output_path: Path,
    force: bool = False,
) -> Path:
    if not input_path.is_file():
        msg = f"config file not found: {input_path}"
        raise FileNotFoundError(msg)
    if input_path.suffix != ".json":
        msg = f"migration input must be a JSON config file: {input_path}"
        raise ValueError(msg)
    if output_path.exists() and not force:
        msg = f"TOML config already exists: {output_path}; use --force to overwrite"
        raise FileExistsError(msg)

    config = read_config_file(input_path, warn_legacy_json=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        tomli_w.dumps(group_config_for_toml(config)), encoding="utf-8"
    )
    # The config may hold credentials; keep it readable only by the user.
    output_path.chmod(0o600)
    return output_path


def migrate_config_command(args: Namespace, parser: ArgumentParser) -> None:
    input_path = Path(args.input) if args.input else discover_json_migration_input()
    if input_path is None:
        parser.error(
            "no legacy config.json found; pass --input to choose a file explicitly"
        )

    output_path = Path(args.output) if args.output else input_path.with_suffix(".toml")
    try:
        migrated_path = migrate_json_config(input_path, output_path, args.force)
    except (FileNotFoundError, FileExistsError, ValueError) as error:
        parser.error(str(error))
    print(f"Wrote TOML config to {migrated_path}")


def check_config_command(args: Namespace, parser: ArgumentParser) -> None:
    config_paths = config_check_paths(args, parser)
    valid = True
    for config_path in config_paths:
        try:
            read_config_file(config_path)
        except ConfigValidationError as error:
            valid = False
            print(f"Config is invalid: {config_path}", file=sys.stderr)
            for detail in error.errors:
                print(f"- {detail}", file=sys.stderr)
        except (OSError, ValueError) as error:
            valid = False
            print(f"Could not read config: {config_path}: {error}", file=sys.stderr)
    if not valid:
        raise SystemExit(1)
    print(f"Config is valid: {format_config_paths(config_paths)}")


def config_check_paths(args: Namespace, parser: ArgumentParser) -> list[Path]:
    if args.config:
        config_path = Path(args.config)
        if not config_path.is_file():
            parser.error(f"config file not found: {args.config}")
        return [config_path]

    config_paths = discover_config_files()
    if not config_paths:
        parser.error(
            "no config.toml or config.json found; pass --config to choose a file"
        )
    return config_paths


def format_config_paths(paths: list[Path]) -> str:
    return ", ".join(str(path) for path in paths)


def run_config_command(args: Namespace, parser: ArgumentParser) -> None:
    if args.config_command == "migrate":
        migrate_config_command(args, parser)
        return
    if args.config_command == "check":
        check_config_command(args, parser)
        return
    parser.error(f"unknown config command: {args.config_command}")


def apply_cli_overrides(config: ConfigDict, args: Namespace) -> None:
    for option in CONFIG_OPTIONS:
        cli = option.cli
        if cli is None:
            continue
        value = getattr(args, cli.dest, None)
        if cli.value_kind == "flag":
            if value:
                config[option.canonical_key] = cli.flag_value
        elif value is not None:
            if cli.value_kind == "csv":
                config[option.canonical_key] = value.split(",")
            else:
                config[option.canonical_key] = value


def get_or_prompt_keyring_secret(
    keyring_backend: Any,
    key_name: Any,
    prompt: str,
    fallback: Any,
) -> Any:
    secret = keyring_backend.get_password("syncmymoodle", key_name)
    if secret is not None:
        return secret

    secret = fallback if fallback else getpass.getpass(prompt)
    keyring_backend.set_password("syncmymoodle", key_name, secret)
    return secret


def resolve_keyring_credentials(
    config: Config,
    file_config: ConfigDict,
    keyring_backend: Any,
) -> None:
    """Fill config.password/totp_secret from the keyring (prompting once).

    ``file_config`` is the merged file-only config, used to insist that
    secrets are not stored in config files; CLI-provided values are allowed
    and seed the keyring on first use.
    """
    if not keyring_backend:
        logger.critical(
            "use_keyring is enabled, but the keyring package is not installed!"
        )
        sys.exit(1)

    if file_config.get("auth.password"):
        logger.critical("You need to remove your password from your config file!")
        sys.exit(1)

    if config.keyring_store_totp_secret and file_config.get("auth.totp_secret"):
        logger.critical("You need to remove your TOTP secret from your config file!")
        sys.exit(1)

    if not config.user:
        print("You need to provide your username in the config file or through --user!")
        sys.exit(1)

    if config.keyring_store_totp_secret and not config.totp_serial:
        print(
            "You need to provide your TOTP provider in the config file or "
            "through --totp-serial!"
        )
        sys.exit(1)

    config.password = get_or_prompt_keyring_secret(
        keyring_backend,
        config.user,
        "Password:",
        config.password,
    )

    if config.keyring_store_totp_secret:
        config.totp_secret = get_or_prompt_keyring_secret(
            keyring_backend,
            config.totp_serial,
            "TOTP-Secret:",
            config.totp_secret,
        )


def validate_required_credentials(config: Config) -> None:
    if not config.user or not config.password:
        logger.critical(
            "You need to specify your username and password in the config file or as an argument!"
        )
        sys.exit(1)
    if not config.totp_serial:
        logger.critical(
            "You need to specify your TOTP generator in the config file or as an argument!"
        )
        sys.exit(1)


def config_from_args(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any = None,
) -> Config:
    file_config = load_config(args, parser)
    merged = dict(file_config)
    apply_cli_overrides(merged, args)
    config = Config.from_dict(merged)
    if config.use_keyring:
        resolve_keyring_credentials(config, file_config, keyring_backend)
    validate_required_credentials(config)
    return config


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser(keyring)
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.loglevel)
    if args.command == "config":
        run_config_command(args, parser)
        return
    run(SyncContext(config=config_from_args(args, parser, keyring)))


def run(ctx: SyncContext) -> None:
    """Execute a full sync run against an already-configured context."""
    print("Logging in...")
    rwth.login(ctx, logger)
    wstoken = moodle_api.get_moodle_wstoken(ctx.session, logger)
    ctx.wstoken = wstoken
    ctx.user_id, ctx.user_private_access_key = moodle_api.get_userid(
        ctx.require_session(), wstoken, logger
    )
    print("Syncing file tree...")
    sync.sync(ctx)
    print("Downloading files...")
    downloader.download_all_files(ctx, logger)
    print("Saving root node as cache...")
    course_cache.cache_root_node(ctx, logger)

    # If we saw multiple Opencast backend errors send a reminder
    # to check the RWTH ITC status page before filing a bug.
    try:
        if ctx.opencast_error_count >= 5:
            logger.warning(
                "Multiple Opencast backend errors occurred. Please check the RWTH "
                "ITC status page before reporting an issue on GitHub: "
                f"{RWTH_MOODLE_STATUS_URL}"
            )
    except Exception:
        # Never let summary logging break the main flow.
        pass


if __name__ == "__main__":
    main()
