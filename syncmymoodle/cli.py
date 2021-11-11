import asyncio
import getpass
import json
import logging
import os
import shutil
import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import TYPE_CHECKING

from syncmymoodle.sync import SyncMyMoodle

try:
    import secretstorage
except ImportError:
    if not TYPE_CHECKING:
        # For some reason mypy in the CI behaves different.
        # Therefore an ignore hint would be marked as superfluous there.
        secretstorage = None


logger = logging.getLogger(__name__)


async def main() -> None:
    parser = ArgumentParser(
        prog="python3 -m syncmymoodle",
        description="Synchronization client for RWTH Moodle. All optional arguments override those in config.json.",
    )
    if secretstorage:
        parser.add_argument(
            "--secretservice",
            action="store_true",
            help="Use FreeDesktop.org Secret Service as storage/retrival for username/passwords.",
        )
    parser.add_argument("--user", default=None, help="Your RWTH SSO username")
    parser.add_argument("--password", default=None, help="Your RWTH SSO password")
    parser.add_argument("--config", default=None, help="The path to the config file")
    parser.add_argument(
        "--cookiefile", default=None, help="The location of the cookie file"
    )
    parser.add_argument(
        "--courses",
        default=None,
        help="Only these courses will be synced (comma seperated links) (if empty, all courses will be synced)",
    )
    parser.add_argument(
        "--skipcourses",
        default=None,
        help="These courses will NOT be synced (comma seperated links)",
    )
    parser.add_argument(
        "--semester",
        default=None,
        help="Only these semesters will be synced, of the form 20ws (comma seperated) (only used if [courses] is empty, if empty all semesters will be synced)",
    )
    parser.add_argument(
        "--basedir",
        default=None,
        help="The base directory where all files will be synced to",
    )
    parser.add_argument(
        "--nolinks",
        action="store_true",
        help="Wether to not inspect links embedded in pages",
    )
    parser.add_argument(
        "--excludefiletypes",
        default=None,
        help='Exclude downloading files from urls with these extensions (comma seperated types, e.g. "mp4,mkv")',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not download any files, instead just perform the synchronization",
    )
    parser.add_argument(
        "--verbose",
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.INFO,
        help="Verbose output for debugging.",
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

    config["user"] = args.user or config.get("user")
    config["password"] = args.password or config.get("password")
    config["cookie_file"] = args.cookiefile or config.get("cookie_file", "./session")
    config["selected_courses"] = (
        args.courses.split(",") if args.courses else config.get("selected_courses", [])
    )
    config["only_sync_semester"] = (
        args.semester.split(",")
        if args.semester
        else config.get("only_sync_semester", [])
    )
    config["basedir"] = args.basedir or config.get("basedir", "./")
    config["use_secret_service"] = (
        args.secretservice if secretstorage else None
    ) or config.get("use_secret_service")
    config["skip_courses"] = (
        args.skipcourses.split(",")
        if args.skipcourses
        else config.get("skip_courses", [])
    )
    config["nolinks"] = args.nolinks or config.get("no_links")
    config["used_modules"] = config.get("used_modules") or {
        "assign": True,
        "resource": True,
        "url": {"youtube": True, "opencast": True, "sciebo": True, "quiz": False},
        "folder": True,
    }
    config["exclude_filetypes"] = (
        args.excludefiletypes.split(",")
        if args.excludefiletypes
        else config.get("exclude_filetypes", [])
    )

    logging.basicConfig(level=args.loglevel, format="%(levelname)s: %(message)s")

    if not shutil.which("wkhtmltopdf") and config["used_modules"]["url"]["quiz"]:
        config["used_modules"]["url"]["quiz"] = False
        logger.warning(
            "You do not have wkhtmltopdf in your path. Quiz-PDFs are NOT generated"
        )

    if secretstorage and config.get("use_secret_service"):
        if config.get("password"):
            logger.critical("You need to remove your password from your config file!")
            sys.exit(1)

        connection = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(connection)
        attributes = {"application": "syncMyMoodle"}
        results = list(collection.search_items(attributes))
        if len(results) == 0:
            if args.password:
                password = args.password
            else:
                password = getpass.getpass("Password:")
            if not args.user and not config.get("user"):
                logger.info(
                    "You need to provide your username in the config file or through --user!"
                )
                sys.exit(1)
            attributes["username"] = config["user"]
            item = collection.create_item(
                f'{config["user"]}@rwth-aachen.de', attributes, password
            )
        else:
            item = results[0]
        if item.is_locked():
            item.unlock()
        if not config.get("user"):
            config["user"] = item.get_attributes().get("username")
        config["password"] = item.get_secret().decode("utf-8")

    if not config.get("user") or not config.get("password"):
        logger.critical(
            "You need to specify your username and password in the config file or as an argument!"
        )
        sys.exit(1)

    loop = asyncio.get_running_loop()
    loop.slow_callback_duration = 0.5

    async with SyncMyMoodle(config) as smm:
        logger.info("Logging in...")
        await smm.login()
        logger.info("Syncing file tree...")
        await smm.sync()

        if args.dry_run:
            logging.info("The following virtual filetree has been generated")
            for file in smm.root_node.list_files():
                print(file)
            sys.exit(0)

        logger.info("Downloading files...")
        await smm.download_all_files()
