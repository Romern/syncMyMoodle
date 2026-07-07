#!/usr/bin/env python3

import copy
import getpass
import json
import logging
import os
import sys
import tomllib
from argparse import ArgumentParser, Namespace
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import tomli_w

from syncmymoodle import course_cache, downloader, rwth, sync
from syncmymoodle import moodle as moodle_api
from syncmymoodle.config import (
    CONFIG_OPTIONS,
    CONFIG_OPTIONS_BY_FIELD,
    Config,
    ConfigValidationError,
    config_validation_errors,
    expand_config_groups,
    group_config_for_toml,
    validate_config,
)
from syncmymoodle.constants import QUIZ_MODES, RWTH_MOODLE_STATUS_URL
from syncmymoodle.context import SyncContext

try:
    import keyring as imported_keyring

    keyring: ModuleType | None = imported_keyring
except ImportError:
    keyring = None

logger = logging.getLogger(__name__)
ConfigDict = dict[str, Any]
CONFIG_FILENAMES = ("config.toml", "config.json")


def build_parser(keyring_backend: Any = None) -> ArgumentParser:
    parser = ArgumentParser(
        prog="python3 -m syncmymoodle",
        description=(
            "Synchronization client for RWTH Moodle. All optional arguments "
            "override those in config.toml/config.json."
        ),
    )

    if keyring_backend:
        parser.add_argument(
            "--secretservice",
            action="store_true",
            help="Use system's keyring for storing and retrieving account credentials",
        )
        parser.add_argument(
            "--secretservicetotpsecret",
            action="store_true",
            help="Save TOTP secret in keyring",
        )

    parser.add_argument(
        "--user", default=None, help="set your RWTH Single Sign-On username"
    )
    parser.add_argument(
        "--password", default=None, help="set your RWTH Single Sign-On password"
    )
    parser.add_argument(
        "--totp",
        default=None,
        help="set your RWTH Single Sign-On TOTP provider's serial number (see https://idm.rwth-aachen.de/selfservice/MFATokenManager)",
    )
    parser.add_argument(
        "--totpsecret",
        default=None,
        help="(optional) set your RWTH Single Sign-On TOTP provider Secret",
    )
    parser.add_argument("--config", default=None, help="set your configuration file")
    parser.add_argument(
        "--cookiefile", default=None, help="set the location of a cookie file"
    )
    parser.add_argument(
        "--courses",
        default=None,
        help="specify the courses that should be synced using comma-separated links. Defaults to all courses, if no additional restrictions e.g. semester are defined.",
    )
    parser.add_argument(
        "--skipcourses",
        default=None,
        help="exclude specific courses using comma-separated links. Defaults to None.",
    )
    parser.add_argument(
        "--semester",
        default=None,
        help="specify semesters to be synced e.g. `22s`, comma-separated. Defaults to all semesters, if no additional restrictions e.g. courses are defined.",
    )
    parser.add_argument(
        "--basedir",
        default=None,
        help="specify the directory where all files will be synced",
    )
    parser.add_argument(
        "--chromiumpath",
        default=None,
        help="set the path to a Chrome/Chromium/Edge binary for quiz PDF rendering",
    )
    parser.add_argument(
        "--courseprefix",
        choices=CONFIG_OPTIONS_BY_FIELD["course_prefix_handling"].choices,
        default=None,
        help=(
            "handle leading two-character course prefixes in local folder names: "
            "'keep' (default), 'remove', or 'suffix'"
        ),
    )
    parser.add_argument(
        "--nolinks",
        action="store_true",
        help="define whether various links in moodle pages should also be inspected e.g. youtube videos, wikipedia articles",
    )
    parser.add_argument(
        "--excludefiletypes",
        default=None,
        help='specify whether specific file types should be excluded, comma-separated e.g. "mp4,mkv"',
    )
    parser.add_argument(
        "--excludefiles",
        default=None,
        help='exclude specific files using comma-separated patterns e.g. "*.bak,*.tmp"',
    )
    parser.add_argument(
        "--excludelinks",
        default=None,
        help="exclude discovered links using comma-separated URL patterns",
    )
    parser.add_argument(
        "--alloweddomains",
        default=None,
        help="only keep discovered links on these comma-separated domains",
    )
    parser.add_argument(
        "--excludesections",
        default=None,
        help="exclude Moodle sections by comma-separated names, ids or patterns",
    )
    parser.add_argument(
        "--excludemodules",
        default=None,
        help="exclude Moodle modules by comma-separated names, ids, types, URLs or patterns",
    )
    parser.add_argument(
        "--quiz",
        choices=QUIZ_MODES,
        default=None,
        help="save quiz review attempts as 'off', 'html', 'pdf', or 'both'",
    )
    parser.add_argument(
        "--updatefiles",
        action="store_true",
        help="define whether modified files with the same name/path should be redownloaded",
    )
    parser.add_argument(
        "--updatefilesconflict",
        choices=CONFIG_OPTIONS_BY_FIELD["update_files_conflict"].choices,
        default=None,
        help=(
            "define how to handle locally modified files when updating: "
            "'rename' (default) moves the old file aside, 'keep' skips the "
            "update, 'overwrite' replaces the local file"
        ),
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
    check_parser.add_argument(
        "--config",
        default=None,
        help="config file to validate; defaults to discovered config.toml/config.json",
    )
    return parser


def read_json_config(path: Path) -> ConfigDict:
    with path.open() as f:
        return cast(ConfigDict, json.load(f))


def read_toml_config(path: Path) -> ConfigDict:
    with path.open("rb") as f:
        return tomllib.load(f)


def warn_legacy_json_config(path: Path) -> None:
    logger.warning(
        "Loading legacy JSON config %s. TOML config is preferred; run "
        "`syncmymoodle config migrate --input %s` to convert it.",
        path,
        path,
    )


def read_config(path: Path, warn_legacy_json: bool = True) -> ConfigDict:
    if path.suffix == ".toml":
        return expand_config_groups(read_toml_config(path))
    if warn_legacy_json:
        warn_legacy_json_config(path)
    return expand_config_groups(read_json_config(path))


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
    if args.config:
        overwrite_config = Path(args.config)
        if not overwrite_config.is_file():
            # Silently continuing without the explicitly requested file would
            # sync with unintended settings (or crash later); fail fast instead.
            parser.error(f"config file not found: {args.config}")
        try:
            return read_config(overwrite_config)
        except ValueError as error:
            parser.error(str(error))

    config: ConfigDict = {}
    for config_file in discover_config_files():
        config.update(read_config(config_file))

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

    config = group_config_for_toml(read_config(input_path, warn_legacy_json=False))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(tomli_w.dumps(config), encoding="utf-8")
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
    try:
        config: ConfigDict = {}
        for config_path in config_paths:
            config.update(read_config(config_path))
    except OSError as error:
        parser.error(str(error))
    except ValueError as error:
        parser.error(f"could not parse config: {error}")

    errors = config_validation_errors(config)
    if errors:
        print(
            f"Config is invalid: {format_config_paths(config_paths)}", file=sys.stderr
        )
        for validation_error in errors:
            print(f"- {validation_error}", file=sys.stderr)
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


def apply_cli_overrides(
    config: ConfigDict,
    args: Namespace,
    keyring_backend: Any = None,
) -> None:
    for option in CONFIG_OPTIONS:
        if option.cli is None:
            continue
        value = getattr(args, option.cli.arg_name)
        if option.cli.value_kind == "flag":
            if value:
                config[option.canonical_key] = True
        elif value is not None:
            if option.cli.value_kind == "csv":
                config[option.canonical_key] = value.split(",")
            else:
                config[option.canonical_key] = value

    if keyring_backend and getattr(args, "secretservice", False):
        config["use_secret_service"] = True
    if keyring_backend and getattr(args, "secretservicetotpsecret", False):
        config["secret_service_store_totp_secret"] = True
    apply_quiz_cli_override(config, args)


def apply_quiz_cli_override(config: ConfigDict, args: Namespace) -> None:
    if args.quiz is None:
        return

    used_modules = config.get("used_modules")
    if isinstance(used_modules, dict):
        used_modules = copy.deepcopy(used_modules)
    else:
        used_modules = {}

    url_modules = used_modules.get("url")
    if isinstance(url_modules, dict):
        url_modules = copy.deepcopy(url_modules)
    else:
        url_modules = {}

    url_modules["quiz"] = args.quiz
    used_modules["url"] = url_modules
    config["used_modules"] = used_modules


def validate_keyring_config(config: ConfigDict, args: Namespace) -> None:
    if config.get("password"):
        logger.critical("You need to remove your password from your config file!")
        sys.exit(1)

    if config.get("secret_service_store_totp_secret") and config.get("totpsecret"):
        logger.critical("You need to remove your totpsecret from your config file!")
        sys.exit(1)

    if not args.user and not config.get("user"):
        print("You need to provide your username in the config file or through --user!")
        sys.exit(1)

    if (
        config.get("secret_service_store_totp_secret")
        and not args.totp
        and not config.get("totp")
    ):
        print(
            "You need to provide your TOTP provider in the config file or through --totp!"
        )
        sys.exit(1)


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
    config: ConfigDict,
    args: Namespace,
    keyring_backend: Any = None,
) -> None:
    if not keyring_backend or not config.get("use_secret_service"):
        return

    validate_keyring_config(config, args)
    config["password"] = get_or_prompt_keyring_secret(
        keyring_backend,
        config.get("user"),
        "Password:",
        args.password,
    )

    if config.get("secret_service_store_totp_secret"):
        config["totpsecret"] = get_or_prompt_keyring_secret(
            keyring_backend,
            config.get("totp"),
            "TOTP-Secret:",
            args.totpsecret,
        )


def validate_required_credentials(config: ConfigDict) -> None:
    if not config.get("user") or not config.get("password"):
        logger.critical(
            "You need to specify your username and password in the config file or as an argument!"
        )
        sys.exit(1)
    if not config.get("totp"):
        logger.critical(
            "You need to specify your TOTP generator in the config file or as an argument!"
        )
        sys.exit(1)


def validate_config_or_error(config: ConfigDict, parser: ArgumentParser) -> None:
    try:
        validate_config(config)
    except ConfigValidationError as error:
        parser.error(str(error))


def config_from_args(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any = None,
) -> Config:
    config = load_config(args, parser)
    apply_cli_overrides(config, args, keyring_backend)
    validate_config_or_error(config, parser)
    resolve_keyring_credentials(config, args, keyring_backend)
    validate_required_credentials(config)
    return Config.from_dict(config)


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
