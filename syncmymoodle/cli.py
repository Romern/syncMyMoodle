#!/usr/bin/env python3

import getpass
import json
import logging
import os
import sys
from argparse import ArgumentParser, Namespace
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from syncmymoodle import course_cache, downloader, rwth, sync
from syncmymoodle import moodle as moodle_api
from syncmymoodle.config import CONFIG_OPTIONS, CONFIG_OPTIONS_BY_FIELD, Config
from syncmymoodle.constants import RWTH_MOODLE_STATUS_URL
from syncmymoodle.context import SyncContext

try:
    import keyring as imported_keyring

    keyring: ModuleType | None = imported_keyring
except ImportError:
    keyring = None

logger = logging.getLogger(__name__)
ConfigDict = dict[str, Any]


def build_parser(keyring_backend: Any = None) -> ArgumentParser:
    parser = ArgumentParser(
        prog="python3 -m syncmymoodle",
        description="Synchronization client for RWTH Moodle. All optional arguments override those in config.json.",
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
    return parser


def read_json_config(path: Path) -> ConfigDict:
    with path.open() as f:
        return cast(ConfigDict, json.load(f))


def global_config_path() -> Path:
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return xdg_config_home / "syncmymoodle" / "config.json"


def load_config(args: Namespace, parser: ArgumentParser) -> ConfigDict:
    if args.config:
        overwrite_config = Path(args.config)
        if not overwrite_config.is_file():
            # Silently continuing without the explicitly requested file would
            # sync with unintended settings (or crash later); fail fast instead.
            parser.error(f"config file not found: {args.config}")
        return read_json_config(overwrite_config)

    config: ConfigDict = {}
    global_config = global_config_path()
    if global_config.is_file():
        config.update(read_json_config(global_config))

    local_config = Path("config.json")
    if local_config.is_file():
        config.update(read_json_config(local_config))

    return config


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


def config_from_args(
    args: Namespace,
    parser: ArgumentParser,
    keyring_backend: Any = None,
) -> Config:
    config = load_config(args, parser)
    apply_cli_overrides(config, args, keyring_backend)
    resolve_keyring_credentials(config, args, keyring_backend)
    validate_required_credentials(config)
    return Config.from_dict(config)


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser(keyring)
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.loglevel)
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
