#!/usr/bin/env python3

import getpass
import json
import logging
import os
import sys
from argparse import ArgumentParser
from pathlib import Path
from types import ModuleType

from syncmymoodle.app import SyncMyMoodle
from syncmymoodle.constants import COURSE_PREFIX_HANDLING_OPTIONS

try:
    import keyring as imported_keyring

    keyring: ModuleType | None = imported_keyring
except ImportError:
    keyring = None

logger = logging.getLogger(__name__)


def main() -> None:
    parser = ArgumentParser(
        prog="python3 -m syncmymoodle",
        description="Synchronization client for RWTH Moodle. All optional arguments override those in config.json.",
    )

    if keyring:
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
        choices=COURSE_PREFIX_HANDLING_OPTIONS,
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
        choices=["rename", "keep", "overwrite"],
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
    args = parser.parse_args()

    if args.config:
        overwrite_config = Path(args.config)
        if overwrite_config.is_file():
            with overwrite_config.open() as f:
                config = json.load(f)
    else:
        config = {}

        global_config = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path("~/.config").expanduser()))
            / "syncmymoodle"
            / "config.json"
        )
        if global_config.is_file():
            with global_config.open() as f:
                config.update(json.load(f))

        local_config = Path("config.json")
        if local_config.is_file():
            with local_config.open() as f:
                config.update(json.load(f))

    if args.user is not None:
        config["user"] = args.user
    if args.password is not None:
        config["password"] = args.password
    if args.totp is not None:
        config["totp"] = args.totp
    if args.totpsecret is not None:
        config["totpsecret"] = args.totpsecret
    if args.cookiefile is not None:
        config["cookie_file"] = args.cookiefile
    if args.courses is not None:
        config["selected_courses"] = args.courses.split(",")
    if args.semester is not None:
        config["only_sync_semester"] = args.semester.split(",")
    if args.basedir is not None:
        config["basedir"] = args.basedir
    if args.courseprefix is not None:
        config["course_prefix_handling"] = args.courseprefix
    if keyring and args.secretservice:
        config["use_secret_service"] = True
    if keyring and args.secretservicetotpsecret:
        config["secret_service_store_totp_secret"] = True
    if args.skipcourses is not None:
        config["skip_courses"] = args.skipcourses.split(",")
    if args.nolinks:
        config["nolinks"] = True
    if args.excludefiletypes is not None:
        config["exclude_filetypes"] = args.excludefiletypes.split(",")
    if args.updatefiles:
        config["updatefiles"] = True
    if args.updatefilesconflict is not None:
        config["update_files_conflict"] = args.updatefilesconflict

    logging.basicConfig(level=args.loglevel)

    url_modules = (
        config.get("used_modules", {}).get("url")
        if isinstance(config.get("used_modules"), dict)
        else None
    )
    if isinstance(url_modules, dict) and url_modules.get("quiz"):
        logger.warning(
            "Quiz PDF generation is disabled until the pdfkit/wkhtmltopdf "
            "renderer is replaced with a safer implementation."
        )

    if keyring and config.get("use_secret_service"):
        if config.get("password"):
            logger.critical("You need to remove your password from your config file!")
            sys.exit(1)

        if config.get("secret_service_store_totp_secret") and config.get("totpsecret"):
            logger.critical("You need to remove your totpsecret from your config file!")
            sys.exit(1)

        if not args.user and not config.get("user"):
            print(
                "You need to provide your username in the config file or through --user!"
            )
            sys.exit(1)

        if (
            config.get("secretservicetotpsecret")
            and not args.totp
            and not config.get("totp")
        ):
            print(
                "You need to provide your TOTP provider in the config file or through --totp!"
            )
            sys.exit(1)

        config["password"] = keyring.get_password("syncmymoodle", config.get("user"))
        if config["password"] is None:
            if args.password:
                password = args.password
            else:
                password = getpass.getpass("Password:")
            keyring.set_password("syncmymoodle", config.get("user"), password)
            config["password"] = password

        if config.get("secret_service_store_totp_secret"):
            config["totpsecret"] = keyring.get_password(
                "syncmymoodle", config.get("totp")
            )
            if config["totpsecret"] is None:
                if args.totpsecret:
                    totpsecret = args.totpsecret
                else:
                    totpsecret = getpass.getpass("TOTP-Secret:")
                keyring.set_password("syncmymoodle", config.get("totp"), totpsecret)
                config["totpsecret"] = totpsecret

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

    smm = SyncMyMoodle(config)

    print("Logging in...")
    smm.login()
    smm.get_moodle_wstoken()
    smm.get_userid()
    print("Syncing file tree...")
    smm.sync()
    print("Downloading files...")
    smm.download_all_files()
    print("Saving root node as cache...")
    smm.cache_root_node()

    # If we saw multiple Opencast backend errors send a reminder
    # to check the RWTH ITC status page before filing a bug.
    try:
        if smm.ctx.opencast_error_count >= 5:
            logger.warning(
                "Multiple Opencast backend errors occurred. Please check the RWTH "
                "ITC status page before reporting an issue on GitHub: "
                "https://maintenance.itc.rwth-aachen.de/ticket/status/messages/499"
            )
    except Exception:
        # Never let summary logging break the main flow.
        pass


if __name__ == "__main__":
    main()
