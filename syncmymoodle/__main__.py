#!/usr/bin/env python3

import base64
import getpass
import hashlib
import hmac
import http.client
import json
import logging
import os
import pickle
import re
import shutil
import struct
import sys
import time
import urllib.parse
from argparse import ArgumentParser
from contextlib import closing
from fnmatch import fnmatchcase
from pathlib import Path
from typing import List

try:
    import pdfkit
except ImportError:
    pdfkit = None

import requests
import yt_dlp
from bs4 import BeautifulSoup as bs
from tqdm import tqdm

try:
    import keyring
except ImportError:
    keyring = None

YOUTUBE_ID_LENGTH = 11

logger = logging.getLogger(__name__)


"""
To add TOTP functionality without adding external dependencies.
Code taken from:
https://github.com/susam/mintotp
"""


def hotp(key, counter, digits=6, digest="sha1"):
    key = base64.b32decode(key.upper() + "=" * ((8 - len(key)) % 8))
    counter = struct.pack(">Q", counter)
    mac = hmac.new(key, counter, digest).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack(">L", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary)[-digits:].zfill(digits)


def totp(key, time_step=30, digits=6, digest="sha1"):
    return hotp(key, int(time.time() / time_step), digits, digest)


class Node:
    def __init__(
        self,
        name,
        id,
        type,  # noqa: A003 - keep original name for compatibility
        parent,
        url=None,
        additional_info=None,
        timemodified=None,
        etag=None,
        is_downloaded=False,
    ):
        self.name = name
        self.id = id
        self.url = url
        self.type = type
        self.parent = parent
        self.children: List[Node] = []
        # Currently only used for course_id in opencast, auth header in sciebo,
        # and may be extended for other module-specific data.
        self.additional_info = additional_info
        self.timemodified = timemodified
        self.etag = etag
        self.is_downloaded = (
            is_downloaded  # Can also be used to exclude files from being downloaded
        )

    def __repr__(self):
        return f"Node(name={self.name}, id={self.id}, url={self.url}, type={self.type})"

    def add_child(
        self,
        name,
        id,
        type,
        url=None,
        additional_info=None,
        timemodified=None,
        etag=None,
    ):
        if url:
            url = url.replace("?forcedownload=1", "").replace(
                "mod_page/content/3", "mod_page/content"
            )
            url = url.replace("webservice/pluginfile.php", "pluginfile.php")

        # Check for duplicate urls and just ignore those nodes:
        if url and any([True for c in self.children if c.url == url]):
            return None

        temp = Node(
            name,
            id,
            type,
            self,
            url=url,
            additional_info=additional_info,
            timemodified=timemodified,
            etag=etag,
        )
        self.children.append(temp)
        return temp

    def get_path(self):
        ret = []
        cur = self
        while cur is not None:
            ret.insert(0, cur.name)
            cur = cur.parent
        return ret

    def go_to_path(self, target_path):
        target_node = [self]
        for path_child in target_path:
            if path_child == "":
                continue
            try:
                target_node.append(
                    [
                        node_child
                        for node_child in target_node[-1].children
                        if node_child.name == path_child
                    ][0]
                )
            except IndexError:
                raise Exception("The path is not found in this root node. Wrong path?")
        return target_node[-1]

    def remove_children_nameclashes(self):
        # Check for duplicate filenames

        unclashed_children = []
        # work on copy since deleting from the iterated list breaks stuff
        copy_children = self.children.copy()
        for child in copy_children:
            if child not in self.children:
                continue
            self.children.remove(child)
            unclashed_children.append(child)
            if child.type == "Opencast":
                siblings = [
                    c
                    for c in self.children
                    if c.name == child.name and c.url != child.url
                ]
                if len(siblings) > 0:
                    # if an Opencast filename is duplicate in its directory, we append the filename as it was uploaded
                    tmp_name = Path(child.name).name
                    child.name = f"{tmp_name}_{child.url.split('/')[-1]}"
                    for s in siblings:
                        tmp_name = Path(s.name).name
                        s.name = f"{s.name}_{s.url.split('/')[-1]}"
                        self.children.remove(s)
                    unclashed_children.extend(siblings)

        self.children = unclashed_children

        unclashed_children = []
        copy_children = self.children.copy()
        for child in copy_children:
            if child not in self.children:
                continue
            self.children.remove(child)
            unclashed_children.append(child)
            siblings = [
                c for c in self.children if c.name == child.name and c.url != child.url
            ]
            if len(siblings) > 0:
                # if a filename is still duplicate in its directory, we rename it by appending its id (urlsafe base64 so it also works for urls).
                filename = Path(child.name)
                child.name = (
                    filename.stem
                    + "_"
                    + base64.urlsafe_b64encode(
                        hashlib.md5(str(child.id).encode("utf-8"))
                        .hexdigest()
                        .encode("utf-8")
                    ).decode()[:10]
                    + filename.suffix
                )
                for s in siblings:
                    filename = Path(s.name)
                    s.name = (
                        filename.stem
                        + "_"
                        + base64.urlsafe_b64encode(
                            hashlib.md5(str(s.id).encode("utf-8"))
                            .hexdigest()
                            .encode("utf-8")
                        ).decode()[:10]
                        + filename.suffix
                    )
                    self.children.remove(s)
                unclashed_children.extend(siblings)

        self.children = unclashed_children

        for child in self.children:
            # recurse whole tree
            child.remove_children_nameclashes()


class SyncMyMoodle:
    params = {"lang": "en"}  # Titles for some pages differ
    block_size = 1024
    invalid_chars = '~"#%&*:<>?/\\{|}'

    def __init__(self, config):
        self.config = config
        self.session = None
        self.session_key = None
        self.wstoken = None
        self.user_id = None
        self.root_node = None
        # Per-course caches: mapping from course directory path to cached
        # course root node loaded from `.syncmymoodle_cache`.
        self._course_caches = {}
        # Track repeated Opencast errors so we can hint at the RWTH
        # status page without spamming messages
        self._opencast_error_count = 0
        self._opencast_status_hint_logged = False

    def cache_root_node(self):
        """Persist per-course caches into .syncmymoodle_cache files.

        Each course directory beneath basedir receives its own cache file
        containing the course subtree, which makes caching less brittle than
        a single global root cache.
        """
        if not self.root_node:
            return

        for semester_node in self.root_node.children:
            if semester_node.type != "Semester":
                continue
            for course_node in semester_node.children:
                if course_node.type != "Course":
                    continue
                course_path = self.get_sanitized_node_path(course_node)
                course_path.mkdir(parents=True, exist_ok=True)
                cache_path = course_path / ".syncmymoodle_cache"
                with cache_path.open("wb") as f:
                    pickle.dump(course_node, f)

    def _ensure_timemodified_attribute(self, node):
        # Old cached root nodes might not have the timemodified attribute yet.
        if not hasattr(node, "timemodified"):
            node.timemodified = None
        if not hasattr(node, "etag"):
            node.etag = None
        for child in getattr(node, "children", []):
            self._ensure_timemodified_attribute(child)

    def _get_course_node(self, node: Node) -> Node:
        """Return the enclosing course node for the given node."""
        cur = node
        while cur is not None and cur.parent is not None:
            if cur.type == "Course":
                return cur
            cur = cur.parent
        raise Exception("Node is not part of a course subtree")

    def _get_course_cache_root(self, course_node: Node):
        """Load and return the cached course root for the given course node."""
        course_path = self.get_sanitized_node_path(course_node)
        if course_path in self._course_caches:
            return self._course_caches[course_path]

        cache_path = course_path / ".syncmymoodle_cache"
        if not cache_path.exists():
            return None

        with cache_path.open("rb") as f:
            try:
                cached_course_root = pickle.load(f)
                self._ensure_timemodified_attribute(cached_course_root)
            except EOFError:
                return None

        self._course_caches[course_path] = cached_course_root
        return cached_course_root

    def _get_old_node_for(self, node: Node):
        """Return the cached node for this node from the course cache, if any."""
        try:
            course_node = self._get_course_node(node)
        except Exception:
            return None

        cached_course_root = self._get_course_cache_root(course_node)
        if cached_course_root is None:
            return None

        full_path = node.get_path()
        course_path = course_node.get_path()
        # Compute the path segments beneath the course root
        rel_segments = full_path[len(course_path) :]
        if not rel_segments:
            return cached_course_root

        try:
            return cached_course_root.go_to_path(rel_segments)
        except Exception:
            return None

    def _make_conflict_path(self, path: Path) -> Path:
        """Return a unique path for storing a locally modified file."""
        suffix = path.suffix
        stem = path.stem

        # Derive a short hash from the current contents to make the filename
        # stable and recognizable while remaining reasonably unique.
        hash_str = "unknown"
        try:
            with path.open("rb") as f:
                digest = hashlib.file_digest(f, "sha1")
                hash_str = digest.hexdigest()[:8]
        except FileNotFoundError:
            hash_str = "missing"

        conflict_path = path.with_name(f"{stem}.syncconflict.{hash_str}{suffix}")
        index = 1
        while conflict_path.exists():
            conflict_path = path.with_name(
                f"{stem}.syncconflict.{hash_str}.{index}{suffix}"
            )
            index += 1
        return conflict_path

    def _local_file_matches_etag(self, path: Path, etag: str) -> bool:
        """Return True if the local file content matches the given ETag hash.

        We currently support strong ETags that contain a plain hex digest for
        MD5 (32 chars), SHA1 (40 chars) or SHA256 (64 chars). Other formats are
        ignored and treated as non-matching.
        """
        # Extract a plausible hex digest from the ETag value, ignoring weak
        # prefixes (W/) and surrounding quotes or algorithm markers.
        match = re.search(r"([0-9a-fA-F]{32,64})", etag)
        if not match:
            return False
        hex_str = match.group(1).lower()

        algo = None
        if len(hex_str) == 32:
            algo = "md5"
        elif len(hex_str) == 40:
            algo = "sha1"
        elif len(hex_str) == 64:
            algo = "sha256"
        else:
            return False

        with path.open("rb") as f:
            digest = hashlib.file_digest(f, algo)
            return digest.hexdigest() == hex_str

    def _log_opencast_backend_issue(self, response_body: str | None = None) -> None:
        """Log additional context for repeated Opencast backend issues.

        We keep the response body at INFO level (only shown with --verbose) and
        emit a hint to the RWTH ITC status page once the error
        counter exceeds a small threshold.
        """
        self._opencast_error_count += 1

        if response_body:
            logger.info(f"Opencast response body (truncated): {response_body[:1000]}")

        if self._opencast_error_count >= 5 and not self._opencast_status_hint_logged:
            logger.warning(
                "Multiple Opencast backend errors occurred. Please check the RWTH "
                "ITC status page before reporting an issue on GitHub: "
                "https://maintenance.itc.rwth-aachen.de/ticket/status/messages/499"
            )
            self._opencast_status_hint_logged = True

    # RWTH SSO Login

    def login(self):
        def get_session_key(soup):
            script = soup.find("script", string=lambda text: text and "sesskey" in text)
            js_text = script.text
            match = re.search(r'"sesskey":"(.*?)"', js_text)
            if match:
                return match.group(1)
            else:
                logger.critical("Can't retrieve session key from JavaScript config")
                exit(1)

        self.session = requests.Session()
        cookie_file = Path(self.config.get("cookie_file", "./session"))
        if cookie_file.exists():
            with cookie_file.open("rb") as f:
                self.session.cookies.update(pickle.load(f))
        resp = self.session.get("https://moodle.rwth-aachen.de/")
        resp = self.session.get(
            "https://moodle.rwth-aachen.de/auth/shibboleth/index.php"
        )
        if resp.url.startswith("https://moodle.rwth-aachen.de/my/"):
            soup = bs(resp.text, features="lxml")
            self.session_key = get_session_key(soup)
            with cookie_file.open("wb") as f:
                pickle.dump(self.session.cookies, f)
            return

        # Create a separate soup for maintenance detection
        soup_check = bs(resp.text, features="lxml")

        # Remove known info banners by class
        for banner in soup_check.select(".themeboostunioninfobanner"):
            banner.decompose()

        # Also remove Bootstrap-style alert boxes marked as informational alerts
        for alert in soup_check.select('div.alert[role="alert"]'):
            alert.decompose()

        # Extract body text after cleanup
        body_text = soup_check.find("body").get_text(separator=" ", strip=True)

        # Check for maintenance notice
        if "Wartungsarbeiten" in body_text:
            logger.critical(
                "Detected Maintenance mode! If this is an error, please report it on GitHub."
            )
            logger.info(f"Cleaned page body:\n{body_text}")
            sys.exit()

        soup = bs(resp.text, features="lxml")
        if soup.find("input", {"name": "RelayState"}) is None:
            csrf_token = soup.find("input", {"name": "csrf_token"})["value"]
            login_data = {
                "j_username": self.config["user"],
                "j_password": self.config["password"],
                "_eventId_proceed": "",
                "csrf_token": csrf_token,
            }
            resp2 = self.session.post(resp.url, data=login_data)

            soup = bs(resp2.text, features="lxml")

            if soup.find(id="fudis_selected_token_ids_input") is None:
                logger.critical(
                    "Failed to login! Maybe your login-info was wrong or the RWTH-Servers have difficulties, see https://maintenance.rz.rwth-aachen.de/ticket/status/messages . For more info use the --verbose argument."
                )
                logger.info("-------Login-Error-Soup--------")
                logger.info(soup)
                sys.exit(1)

            csrf_token = soup.find("input", {"name": "csrf_token"})["value"]

            print("Setting TOTP generator")
            totp_selection_data = {
                "fudis_selected_token_ids_input": self.config["totp"],
                "_eventId_proceed": "",
                "csrf_token": csrf_token,
            }

            resp3 = self.session.post(resp2.url, data=totp_selection_data)

            soup = bs(resp3.text, features="lxml")
            if soup.find(id="fudis_otp_input") is None:
                logger.critical(
                    "Failed to select TOTP generator! Maybe your TOTP serial number is wrong or the RWTH-Servers have difficulties, see https://maintenance.rz.rwth-aachen.de/ticket/status/messages . For more info use the --verbose argument."
                )
                logger.info("-------Login-Error-Soup--------")
                logger.info(soup)
                sys.exit(1)

            csrf_token = soup.find("input", {"name": "csrf_token"})["value"]
            if not self.config.get("totpsecret"):
                totp_input = input(f"Enter TOTP for generator {self.config['totp']}:\n")
            else:
                totp_input = totp(self.config.get("totpsecret"))
                print(f"Generated TOTP from provided secret: {totp_input}")

            totp_login_data = {
                "fudis_otp_input": totp_input,
                "_eventId_proceed": "",
                "csrf_token": csrf_token,
            }

            resp4 = self.session.post(resp3.url, data=totp_login_data)

            time.sleep(1)  # if we go too fast, we might have our connection closed
            soup = bs(resp4.text, features="lxml")
        if soup.find("input", {"name": "RelayState"}) is None:
            logger.critical(
                "Failed to login! Maybe your login-info was wrong or the RWTH-Servers have difficulties, see https://maintenance.rz.rwth-aachen.de/ticket/status/messages . For more info use the --verbose argument."
            )
            logger.info("-------Login-Error-Soup--------")
            logger.info(soup)
            sys.exit(1)
        data = {
            "RelayState": soup.find("input", {"name": "RelayState"})["value"],
            "SAMLResponse": soup.find("input", {"name": "SAMLResponse"})["value"],
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/Shibboleth.sso/SAML2/POST", data=data
        )
        with cookie_file.open("wb") as f:
            soup = bs(resp.text, features="lxml")
            self.session_key = get_session_key(soup)
            pickle.dump(self.session.cookies, f)

    # Moodle Web Services API

    def get_moodle_wstoken(self):
        if not self.session:
            raise Exception("You need to login() first.")
        params = {
            "service": "moodle_mobile_app",
            "passport": 1,
            "urlscheme": "moodlemobile",
        }
        # response = self.session.head("https://moodle.rwth-aachen.de/admin/tool/mobile/launch.php", params=params, allow_redirects=False)

        def getCookies(cookie_jar, domain):
            # workaround for macos
            cookie_dict = cookie_jar.get_dict(domain=domain)
            found = ["%s=%s" % (name, value) for (name, value) in cookie_dict.items()]
            return ";".join(found)

        conn = http.client.HTTPSConnection("moodle.rwth-aachen.de")
        conn.request(
            "GET",
            "/admin/tool/mobile/launch.php?" + urllib.parse.urlencode(params),
            headers={
                "Cookie": getCookies(self.session.cookies, "moodle.rwth-aachen.de")
            },
        )
        response = conn.getresponse()

        # token is in an app schema, which contains the wstoken base64-encoded along with some other token
        token_base64d = response.getheader("Location").split("token=")[1]
        self.wstoken = base64.b64decode(token_base64d).decode().split(":::")[1]
        return self.wstoken

    def get_all_courses(self):
        data = {
            "requests[0][function]": "core_enrol_get_users_courses",
            "requests[0][arguments]": json.dumps(
                {"userid": str(self.user_id), "returnusercount": "0"}
            ),
            "requests[0][settingfilter]": 1,
            "requests[0][settingfileurl]": 1,
            "wsfunction": "tool_mobile_call_external_functions",
            "wstoken": self.wstoken,
        }
        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "tool_mobile_call_external_functions",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        return json.loads(resp.json()["responses"][0]["data"])

    def get_course(self, course_id):
        data = {
            "courseid": int(course_id),
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "core_course_get_contents",
            "wstoken": self.wstoken,
        }
        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "core_course_get_contents",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        return resp.json()

    def get_userid(self):
        data = {
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "core_webservice_get_site_info",
            "wstoken": self.wstoken,
        }
        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "core_webservice_get_site_info",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        if not resp.json().get("userid") or not resp.json()["userprivateaccesskey"]:
            logger.critical(
                f"Error while getting userid and access key: {json.dumps(resp.json(), indent=4)}"
            )
            sys.exit(1)
        self.user_id = resp.json()["userid"]
        self.user_private_access_key = resp.json()["userprivateaccesskey"]
        return self.user_id, self.user_private_access_key

    def get_assignment(self, course_id):
        data = {
            "courseids[0]": int(course_id),
            "includenotenrolledcourses": 1,
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "mod_assign_get_assignments",
            "wstoken": self.wstoken,
        }
        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "mod_assign_get_assignments",
        }
        resp = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        return resp.json()["courses"][0] if len(resp.json()["courses"]) > 0 else None

    def get_assignment_submission_files(self, assignment_id):
        data = {
            "assignid": assignment_id,
            "userid": self.user_id,
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "mod_assign_get_submission_status",
            "wstoken": self.wstoken,
        }

        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "mod_assign_get_submission_status",
        }

        response = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )

        logger.info(f"------ASSIGNMENT-{assignment_id}-DATA------")
        logger.info(response.text)

        files = (
            response.json()
            .get("lastattempt", {})
            .get("submission", {})
            .get("plugins", [])
        )
        files += (
            response.json()
            .get("lastattempt", {})
            .get("teamsubmission", {})
            .get("plugins", [])
        )
        files += response.json().get("feedback", {}).get("plugins", [])

        files = [
            f.get("files", [])
            for p in files
            for f in p.get("fileareas", [])
            if f["area"] in ["download", "submission_files", "feedback_files"]
        ]
        files = [f for folder in files for f in folder]
        return files

    def get_folders_by_courses(self, course_id):
        data = {
            "courseids[0]": str(course_id),
            "moodlewssettingfilter": True,
            "moodlewssettingfileurl": True,
            "wsfunction": "mod_folder_get_folders_by_courses",
            "wstoken": self.wstoken,
        }

        params = {
            "moodlewsrestformat": "json",
            "wsfunction": "mod_folder_get_folders_by_courses",
        }

        response = self.session.post(
            "https://moodle.rwth-aachen.de/webservice/rest/server.php",
            params=params,
            data=data,
        )
        folder = response.json()["folders"]
        return folder

    def sync(self):
        """Retrives the file tree for all courses"""
        if not self.session:
            raise Exception("You need to login() first.")
        if not self.wstoken:
            raise Exception("You need to get_moodle_wstoken() first.")
        if not self.user_id:
            raise Exception("You need to get_userid() first.")
        self.root_node = Node("", -1, "Root", None)

        # Syncing all courses
        for course in self.get_all_courses():
            course_name = course["shortname"]
            course_id = course["id"]

            if (
                len(
                    [
                        c
                        for c in self.config.get("skip_courses", [])
                        if str(course_id) in c
                    ]
                )
                > 0
            ):
                continue

            # Skip not selected courses
            if (
                len(self.config.get("selected_courses", [])) > 0
                and len(
                    [
                        c
                        for c in self.config.get("selected_courses", [])
                        if str(course["id"]) in c
                    ]
                )
                == 0
            ):
                continue

            semestername = course["idnumber"][:4]
            # Skip not selected semesters
            if (
                len(self.config.get("selected_courses", [])) == 0
                and self.config.get("only_sync_semester", [])
                and semestername not in self.config.get("only_sync_semester", [])
            ):
                continue

            semester_node = [
                s for s in self.root_node.children if s.name == semestername
            ]
            if len(semester_node) == 0:
                semester_node = self.root_node.add_child(semestername, None, "Semester")
            else:
                semester_node = semester_node[0]

            course_node = semester_node.add_child(course_name, course_id, "Course")

            print(f"Syncing {course_name}...")
            assignments = self.get_assignment(course_id)
            folders = self.get_folders_by_courses(course_id)

            logger.info("-----------------------")
            logger.info(f"------{semestername} - {course_name}------")
            logger.info("------COURSE-DATA------")
            logger.info(json.dumps(course))
            logger.info("------ASSIGNMENT-DATA------")
            logger.info(json.dumps(assignments))
            logger.info("------FOLDER-DATA------")
            logger.info(json.dumps(folders))

            for section in self.get_course(course_id):
                if isinstance(section, str):
                    logger.error(f"Error syncing section in {course_name}: {section}")
                    continue
                logger.info("------SECTION-DATA------")
                logger.info(json.dumps(section))
                section_node = course_node.add_child(
                    section["name"], section["id"], "Section"
                )
                for module in section["modules"]:
                    try:
                        # Get Assignments
                        if module["modname"] == "assign" and self.config.get(
                            "used_modules", {}
                        ).get("assign", {}):
                            if assignments is None:
                                continue
                            ass = [
                                a
                                for a in assignments.get("assignments")
                                if a["cmid"] == module["id"]
                            ]
                            if len(ass) == 0:
                                continue
                            ass = ass[0]
                            assignment_id = ass["id"]
                            assignment_name = module["name"]
                            assignment_node = section_node.add_child(
                                assignment_name, assignment_id, "Assignment"
                            )

                            ass = ass[
                                "introattachments"
                            ] + self.get_assignment_submission_files(assignment_id)
                            for c in ass:
                                if c["filepath"] != "/":
                                    assignment_node.add_child(
                                        str(
                                            Path(
                                                self.sanitize(c["filepath"]),
                                                self.sanitize(c["filename"]),
                                            )
                                        ),
                                        c["fileurl"],
                                        "Assignment File",
                                        url=c["fileurl"],
                                        timemodified=c.get("timemodified"),
                                    )
                                else:
                                    assignment_node.add_child(
                                        c["filename"],
                                        c["fileurl"],
                                        "Assignment File",
                                        url=c["fileurl"],
                                        timemodified=c.get("timemodified"),
                                    )

                        # Get Resources or URLs
                        if module["modname"] in [
                            "resource",
                            "url",
                            "book",
                            "page",
                            "pdfannotator",
                        ]:
                            if module["modname"] == "resource" and not self.config.get(
                                "used_modules", {}
                            ).get("resource", {}):
                                continue
                            for c in module.get("contents", []):
                                if c["fileurl"]:
                                    self.scanForLinks(
                                        c["fileurl"],
                                        section_node,
                                        course_id,
                                        single=True,
                                        module_title=module["name"],
                                    )

                        # Get Folders
                        if module["modname"] == "folder" and self.config.get(
                            "used_modules", {}
                        ).get("folder", {}):
                            folder_node = section_node.add_child(
                                module["name"], module["id"], "Folder"
                            )

                            # Scan intro for links
                            rel_folder = [
                                f["intro"]
                                for f in folders
                                if f["coursemodule"] == module["id"]
                            ]
                            if rel_folder:
                                self.scanForLinks(rel_folder[0], folder_node, course_id)

                            for c in module.get("contents", []):
                                if c["filepath"] != "/":
                                    while c["filepath"][-1] == "/":
                                        c["filepath"] = c["filepath"][:-1]
                                    while c["filepath"][0] == "/":
                                        c["filepath"] = c["filepath"][1:]
                                    folder_node.add_child(
                                        str(
                                            Path(
                                                self.sanitize(c["filepath"]),
                                                self.sanitize(c["filename"]),
                                            )
                                        ),
                                        c["fileurl"],
                                        "Folder File",
                                        url=c["fileurl"],
                                        timemodified=c.get("timemodified"),
                                    )
                                else:
                                    folder_node.add_child(
                                        c["filename"],
                                        c["fileurl"],
                                        "Folder File",
                                        url=c["fileurl"],
                                        timemodified=c.get("timemodified"),
                                    )

                        # Get embedded videos in pages or labels
                        if module["modname"] in [
                            "page",
                            "label",
                            "h5pactivity",
                        ] and self.config.get("used_modules", {}).get("url", {}):
                            if module["modname"] == "page":
                                self.scanForLinks(
                                    module["url"],
                                    section_node,
                                    course_id,
                                    module_title=module["name"],
                                    single=True,
                                )
                            # "Interactive" h5p videos
                            elif module["modname"] == "h5pactivity":
                                html_url = f'https://moodle.rwth-aachen.de/mod/h5pactivity/view.php?id={module["id"]}'
                                html = bs(
                                    self.session.get(html_url).text,
                                    features="lxml",
                                )
                                # Get h5p iframe
                                iframe = html.find("iframe")
                                if iframe is not None:
                                    iframe_html = str(
                                        bs(
                                            self.session.get(iframe.attrs["src"]).text,
                                            features="lxml",
                                        )
                                    )
                                    # Moodle devs dont know how to use CDATA correctly, so we need to remove all backslashes
                                    sanitized_html = iframe_html.replace("\\", "")
                                else:
                                    # H5P outside iframes
                                    sanitized_html = str(html).replace("\\", "")

                                self.scanForLinks(
                                    sanitized_html,
                                    section_node,
                                    course_id,
                                    module_title=module["modname"],
                                    single=False,
                                )
                            else:
                                self.scanForLinks(
                                    module.get("description", ""),
                                    section_node,
                                    course_id,
                                    module_title=module["name"],
                                )

                        # New OpenCast integration
                        if module["modname"] == "lti" and self.config.get(
                            "used_modules", {}
                        ).get("url", {}).get("opencast", {}):
                            info_url = f'https://moodle.rwth-aachen.de/mod/lti/launch.php?id={module["id"]}&triggerview=0'
                            info_res = bs(
                                self.session.get(info_url).text, features="lxml"
                            )
                            # FIXME: For now we assume that all lti modules will lead to an opencast video
                            engage_id = info_res.find("input", {"name": "custom_id"})
                            name = info_res.find(
                                "input", {"name": "resource_link_title"}
                            )
                            if not engage_id:
                                logger.error("Failed to find custom_id on lti page.")
                                logger.info("------LTI-ERROR-HTML------")
                                logger.info(f"url: {info_url}")
                                logger.info(info_res)
                            else:
                                engage_id = engage_id.get("value")
                                name = name.get("value")
                                vid = self.getOpenCastRealURL(
                                    course_id,
                                    f"https://engage.streaming.rwth-aachen.de/play/{engage_id}",
                                )
                                section_node.add_child(
                                    name,
                                    engage_id,
                                    "Opencast",
                                    url=vid,
                                    additional_info=course_id,
                                )
                        # Integration for Quizzes
                        if module["modname"] == "quiz" and self.config.get(
                            "used_modules", {}
                        ).get("url", {}).get("quiz", {}):
                            info_url = f'https://moodle.rwth-aachen.de/mod/quiz/view.php?id={module["id"]}'
                            info_res = bs(
                                self.session.get(info_url).text, features="lxml"
                            )
                            attempts = info_res.findAll(
                                "a",
                                {
                                    "title": "Überprüfung der eigenen Antworten dieses Versuchs"
                                },
                            )
                            attempt_cnt = 0
                            for attempt in attempts:
                                attempt_cnt += 1
                                review_url = attempt.get("href")
                                quiz_res = bs(
                                    self.session.get(review_url).text,
                                    features="lxml",
                                )
                                name = (
                                    quiz_res.find("title")
                                    .get_text()
                                    .replace(": Überprüfung des Testversuchs", "")
                                    + ", Versuch "
                                    + str(attempt_cnt)
                                )
                                section_node.add_child(
                                    self.sanitize(name),
                                    urllib.parse.urlparse(review_url)[1],
                                    "Quiz",
                                    url=review_url,
                                )

                    except Exception:
                        logger.exception(f"Failed to download the module {module}")

        self.root_node.remove_children_nameclashes()

    def download_all_files(self):
        if not self.session:
            raise Exception("You need to login() first.")
        if not self.wstoken:
            raise Exception("You need to get_moodle_wstoken() first.")
        if not self.user_id:
            raise Exception("You need to get_userid() first.")
        if not self.root_node:
            raise Exception("You need to sync() first.")

        self._download_all_files(self.root_node)

    def _download_all_files(self, cur_node):
        if len(cur_node.children) == 0:
            if cur_node.url and not cur_node.is_downloaded:
                if cur_node.type == "Youtube":
                    try:
                        self.scanAndDownloadYouTube(cur_node)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                        logger.error(
                            "This could be caused by an out of date yt-dlp version. Try upgrading yt-dlp through pip or your package manager."
                        )
                elif cur_node.type == "Opencast":
                    try:
                        self.downloadOpenCastVideos(cur_node)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                elif cur_node.type == "Quiz":
                    try:
                        self.downloadQuiz(cur_node)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
                        logger.warning("Is wkhtmltopdf correctly installed?")
                else:
                    try:
                        self.download_file(cur_node)
                        cur_node.is_downloaded = True
                    except Exception:
                        logger.exception(f"Failed to download the module {cur_node}")
            return

        for child in cur_node.children:
            self._download_all_files(child)

    def get_sanitized_node_path(self, node: Node) -> Path:
        basedir = Path(self.config.get("basedir", "./")).expanduser()
        return basedir.joinpath(*(self.sanitize(p) for p in node.get_path()))

    def sanitize(self, path):
        path = urllib.parse.unquote(path)
        path = "".join([s for s in path if s not in self.invalid_chars])
        while path and path[-1] == " ":
            path = path[:-1]
        while path and path[0] == " ":
            path = path[1:]

        # Folders downloaded from Moodle display amp; in places where an
        # ampersand should be displayed instead. In the web UI, however, the
        # ampersand is shown correctly, and we're trying to emulate that here.
        path = path.replace("amp;", "&")

        return path

    def download_file(self, node):
        """Download file with progress bar if it isn't already downloaded"""
        downloadpath = self.get_sanitized_node_path(node)

        # If we already downloaded this path during the current run, skip any
        # further processing. This avoids duplicate downloads and spurious
        # conflicts when the same remote file appears multiple times in the
        # node tree (e.g. Sciebo links reused in a course).
        if hasattr(self, "_downloaded_paths"):
            if downloadpath in self._downloaded_paths:
                return True
        else:
            # Initialise on first use to keep __init__ simple.
            self._downloaded_paths = set()

        # Decide whether we need to (re-)download the file at all
        cached_timemodified = None
        old_node = None
        if downloadpath.exists():
            if not self.config.get("updatefiles"):
                return True

            # Try to find a cached node for this file from the per-course cache.
            old_node = self._get_old_node_for(node)
            if old_node is not None:
                cached_timemodified = getattr(old_node, "timemodified", None)
                old_etag = getattr(old_node, "etag", None)
                # If Moodle did not change the file, skip re-download.
                if node.timemodified == cached_timemodified:
                    return True
                # For Sciebo, we use the etag from the previous run as the
                # remote version marker. If it matches the current etag from
                # the PROPFIND response, the remote file has not changed.
                if (
                    cached_timemodified is None
                    and old_etag
                    and getattr(node, "etag", None) == old_etag
                ):
                    # Additionally, on the first run with a cache, the local file
                    # may already match this etag (e.g. previously downloaded
                    # manually). If so, we can safely skip any download.
                    if self._local_file_matches_etag(downloadpath, old_etag):
                        return True

            # At this point, either there is no cache for this course/path, or
            # Moodle reports a different modification time. This means the
            # remote file might have changed.

            # Check for potential local modifications since the last sync to avoid
            # silently overwriting user changes.
            conflict_mode = self.config.get("update_files_conflict", "rename")
            if conflict_mode not in {"rename", "keep", "none", "overwrite"}:
                conflict_mode = "rename"

            local_conflict = False
            old_etag = getattr(old_node, "etag", None) if old_node is not None else None
            if old_etag:
                # Prefer using the old ETag (hash) to detect whether the local file
                # still matches the previously downloaded version.
                try:
                    if not self._local_file_matches_etag(downloadpath, old_etag):
                        local_conflict = True
                except Exception:
                    # If we cannot safely compare using the ETag, fall back to the
                    # timestamp-based heuristic below.
                    local_conflict = False

            if not old_etag:
                if cached_timemodified is not None:
                    # Fallback: compare local mtime with the previous Moodle timestamp.
                    try:
                        local_mtime = int(downloadpath.stat().st_mtime)
                        if local_mtime != int(cached_timemodified):
                            local_conflict = True
                    except (OSError, ValueError):
                        local_conflict = True
                else:
                    # No previous etag and no previous timemodified: this usually
                    # means the file existed before we ever cached it. Before we
                    # treat this as a conflict, try to see if the local file
                    # already matches the *current* remote content using the
                    # ETag from either the Sciebo PROPFIND or a Moodle HEAD
                    # request.
                    remote_etag = getattr(node, "etag", None)
                    if remote_etag is None and node.url:
                        try:
                            head_resp = self.session.head(
                                node.url, allow_redirects=True
                            )
                            remote_etag = head_resp.headers.get("ETag")
                        except Exception:
                            remote_etag = None

                    if remote_etag and self._local_file_matches_etag(
                        downloadpath, remote_etag
                    ):
                        # Local file already equals the current remote content,
                        # so there is no conflict and no need to download again.
                        node.etag = remote_etag
                        if getattr(node, "timemodified", None) is not None:
                            try:
                                ts = int(node.timemodified)
                                os.utime(downloadpath, (ts, ts))
                            except (OSError, OverflowError, ValueError):
                                pass
                        return True

                    # At this point we know the local file differs from the
                    # current remote version (or we couldn't verify), and we
                    # have no prior cached state. Treat this as a potential
                    # conflict to avoid silently overwriting user changes.
                    local_conflict = True

            if local_conflict:
                if conflict_mode in {"keep", "none"}:
                    # Keep the locally modified file and skip updating from Moodle
                    logger.info(
                        "Detected local changes for %s, skipping Moodle update "
                        "due to update_files_conflict=%s",
                        downloadpath,
                        conflict_mode,
                    )
                    return True
                if conflict_mode == "rename":
                    # Move the locally modified file out of the way before download
                    conflict_path = self._make_conflict_path(downloadpath)
                    try:
                        downloadpath.rename(conflict_path)
                        logger.warning(
                            "Detected local changes for %s, moving to %s before "
                            "downloading updated file from Moodle",
                            downloadpath,
                            conflict_path,
                        )
                    except OSError:
                        logger.exception(
                            "Failed to move locally modified file %s to %s, "
                            "skipping Moodle update to avoid data loss",
                            downloadpath,
                            conflict_path,
                        )
                        return True
                # conflict_mode == "overwrite": fall through and overwrite

        if len(node.name.split(".")) > 0 and node.name.split(".")[
            -1
        ] in self.config.get("exclude_filetypes", []):
            return True

        if any(
            fnmatchcase(node.name, pattern)
            for pattern in self.config.get("exclude_files")
        ):
            return True

        tmp_downloadpath = downloadpath.with_suffix(downloadpath.suffix + ".temp")
        if tmp_downloadpath.exists():
            resume_size = tmp_downloadpath.stat().st_size
            header = {"Range": f"bytes={resume_size}-"}
        else:
            resume_size = 0
            header = dict()
        if node.type.lower() == "sciebo file":
            header = {**header, **node.additional_info}

        with closing(
            self.session.get(node.url, headers=header, stream=True)
        ) as response:
            etag_header = response.headers.get("ETag")
            print(f"Downloading {downloadpath} [{node.type}]")
            # If we attempted to resume but the server did not honor the Range
            # header (status != 206), fallback to a full download and ignore
            # the existing partial file to avoid corrupting PDFs or other
            # content by appending a second full copy.
            if resume_size and response.status_code != 206:
                resume_size = 0
                tmp_downloadpath.unlink(missing_ok=True)

            total_size_in_bytes = int(response.headers.get("content-length", 0)) + max(
                resume_size, 0
            )
            progress_bar = tqdm(total=total_size_in_bytes, unit="iB", unit_scale=True)
            if resume_size:
                progress_bar.update(resume_size)
            downloadpath.parent.mkdir(parents=True, exist_ok=True)
            mode = "ab" if resume_size else "wb"
            with tmp_downloadpath.open(mode) as file:
                for data in response.iter_content(self.block_size):
                    progress_bar.update(len(data))
                    file.write(data)
            progress_bar.close()
            tmp_downloadpath.rename(downloadpath)
            # Align the local mtime with Moodle's timemodified to detect local
            # changes on subsequent runs.
            if getattr(node, "timemodified", None) is not None:
                try:
                    ts = int(node.timemodified)
                    os.utime(downloadpath, (ts, ts))
                except (OSError, OverflowError, ValueError):
                    # If updating timestamps fails, fall back to the current time.
                    pass
            # Persist the ETag of the downloaded file on the node so it can be
            # used on the next run to detect local modifications.
            if etag_header is not None:
                try:
                    node.etag = etag_header
                except Exception:
                    # If for some reason we cannot set it, just ignore.
                    pass
            # Remember that we downloaded this path during the current run.
            self._downloaded_paths.add(downloadpath)
            return True

    def getOpenCastRealURL(self, additional_info, url):
        """Download Opencast videos by using the engage API"""
        # get engage authentication form
        course_info = [
            {
                "index": 0,
                "methodname": "filter_opencast_get_lti_form",
                "args": {"courseid": str(additional_info)},
            }
        ]
        response = self.session.post(
            f"https://moodle.rwth-aachen.de/lib/ajax/service.php?sesskey={self.session_key}&info=filter_opencast_get_lti_form",
            data=json.dumps(course_info),
        )

        # submit engage authentication info
        try:
            engageDataSoup = bs(response.json()[0]["data"], features="lxml")
        except Exception as e:
            logger.exception("Failed to parse Opencast response!")
            logger.info("------Opencast-Error------")
            logger.info(response.text)
            raise e

        engageData = dict(
            [(i["name"], i["value"]) for i in engageDataSoup.findAll("input")]
        )
        response = self.session.post(
            "https://engage.streaming.rwth-aachen.de/lti", data=engageData
        )

        linkid = re.match(
            "https://engage.streaming.rwth-aachen.de/play/([a-z0-9-]{36})$", url
        )
        if not linkid:
            logger.warning(f"Opencast: could not extract episode id from url {url}")
            return False

        episode_url = (
            "https://engage.streaming.rwth-aachen.de/search/episode.json"
            f"?id={linkid.groups()[0]}"
        )
        try:
            episode_response = self.session.get(episode_url)
        except Exception:
            logger.exception(
                "Opencast: failed to fetch episode metadata from %s", episode_url
            )
            self._log_opencast_backend_issue(None)
            return False

        if not (200 <= episode_response.status_code < 300):
            logger.error(
                "Opencast: episode.json returned status %s for %s",
                episode_response.status_code,
                episode_url,
            )
            self._log_opencast_backend_issue(episode_response.text)
            return False

        try:
            episodejson = episode_response.json()
        except ValueError:
            logger.error("Opencast: failed to decode JSON from %s", episode_url)
            self._log_opencast_backend_issue(episode_response.text)
            return False

        # Collect tracks from all mediapackages
        mediapackages = [
            track["mediapackage"]["media"]["track"] for track in episodejson["result"]
        ]

        # TODO, handle multiple mediapackages (videos? could be seperate presenter and screencap)
        tracks = mediapackages[0]

        # Filter and sort tracks by resolution (width)
        tracks = sorted(
            [
                (t["url"], t["video"]["resolution"])
                for t in tracks
                if t["mimetype"] == "video/mp4"
                and "transport" not in t
                and "video" in t
            ],
            key=lambda x: int(x[1].split("x")[0]),  # Sort by width (e.g., "1920x1080")
        )

        # only choose mp4s provided with plain https (no transport key), and use the one with the highest resolution (sorted by width) (could also use bitrate)
        finaltrack = tracks[-1]

        return finaltrack[0]

    def downloadOpenCastVideos(self, node):
        if ".mp4" not in node.name:
            if node.name is not None and node.name != "":
                node.name += ".mp4"
            else:
                node.name = node.url.split("/")[-1]
        return self.download_file(node)

    def scanAndDownloadYouTube(self, node):
        """Download Youtube-Videos using yt_dlp"""
        path = self.get_sanitized_node_path(node.parent)
        link = node.url
        if path.exists():
            if any(link[-YOUTUBE_ID_LENGTH:] in f.name for f in path.iterdir()):
                return False
        ydl_opts = {
            "outtmpl": "{}/%(title)s-%(id)s.%(ext)s".format(path),
            "ignoreerrors": True,
            "nooverwrites": True,
            "retries": 15,
            "match_filter": yt_dlp.match_filter_func("!is_live"),
        }
        path.mkdir(parents=True, exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([link])
        return True

    def downloadQuiz(self, node):
        path = self.get_sanitized_node_path(node.parent)
        path.mkdir(parents=True, exist_ok=True)

        if (path / f"{node.name}.pdf").exists():
            return True

        quiz_res = bs(self.session.get(node.url).text, features="lxml")

        # i need to hide the left nav element because its obscuring the quiz in the resulting pdf
        for nav in quiz_res.findAll("div", {"id": "nav-drawer"}):
            nav["style"] = "visibility: hidden;"

        quiz_html = str(quiz_res)
        print("Generating quiz-PDF for " + node.name + "... [Quiz]")

        pdfkit.from_string(
            quiz_html,
            path / f"{node.name}.pdf",
            options={
                "quiet": "",
                "javascript-delay": "30000",
                "disable-smart-shrinking": "",
                "run-script": 'MathJax.Hub.Config({"CommonHTML": {minScaleAdjust: 100},"HTML-CSS": {scale: 200}}); MathJax.Hub.Queue(["Rerender", MathJax.Hub], function () {window.status="finished"})',
            },
        )

        print("...done!")
        return True

    def scanForLinks(
        self, text, parent_node, course_id, module_title=None, single=False
    ):
        # A single link is supplied and the contents of it are checked
        if single:
            try:
                text = text.replace("webservice/pluginfile.php", "pluginfile.php")
                response = self.session.head(text)
                if "youtube.com" in text or "youtu.be" in text:
                    # workaround for youtube providing bad headers when using HEAD
                    pass
                elif (
                    "Content-Type" in response.headers
                    and "text/html" not in response.headers["Content-Type"]
                ):
                    # non html links, assume the filename is in the path
                    filename = urllib.parse.urlsplit(text).path.split("/")[-1]
                    parent_node.add_child(
                        filename,
                        None,
                        f'Linked file [{response.headers["Content-Type"]}]',
                        url=text,
                    )
                    # instantly return as it was a direct link
                    return
                elif not self.config.get("nolinks"):
                    response = self.session.get(text)
                    tempsoup = bs(response.text, features="lxml")
                    videojs = tempsoup.select_one(".video-js")
                    if videojs:
                        videojs = videojs.select_one("source")
                        if videojs and videojs.get("src"):
                            parsed = urllib.parse.urlparse(response.url)
                            link = urllib.parse.urljoin(
                                f"{parsed.scheme}://{parsed.netloc}/{parsed.path}",
                                videojs["src"],
                            )
                            parent_node.add_child(
                                videojs["src"].split("/")[-1],
                                None,
                                "Embedded videojs",
                                url=link,
                            )
                    # further inspect the response for other links
                    self.scanForLinks(
                        response.text,
                        parent_node,
                        course_id,
                        module_title=module_title,
                        single=False,
                    )
            except Exception:
                # Maybe the url is down?
                logger.exception(f"Error while downloading url {text}")
        if self.config.get("nolinks"):
            return

        # Youtube videos
        if self.config.get("used_modules", {}).get("url", {}).get("youtube", {}):
            youtube_links = [
                u[0]
                # finds youtube.com, youtu.be and embed links
                for u in re.findall(
                    r"(https?://(www\.)?(youtube\.com/(watch\?[a-zA-Z0-9_=&-]*v=|embed/)|youtu.be/).{11})",
                    text,
                )
            ]
            for link in youtube_links:
                parent_node.add_child(
                    f"Youtube: {module_title or link}", link, "Youtube", url=link
                )

        # OpenCast videos
        if self.config.get("used_modules", {}).get("url", {}).get("opencast", {}):
            opencast_links = re.findall(
                "https://engage.streaming.rwth-aachen.de/play/[a-zA-Z0-9-]+", text
            )
            for vid in opencast_links:
                vid = self.getOpenCastRealURL(course_id, vid)
                parent_node.add_child(
                    module_title or vid.split("/")[-1],
                    vid,
                    "Opencast",
                    url=vid,
                    additional_info=course_id,
                )

        # https://rwth-aachen.sciebo.de/s/XXX
        if self.config.get("used_modules", {}).get("url", {}).get("sciebo", {}):
            sciebo_links = set(
                re.findall("https://rwth-aachen.sciebo.de/s/[a-zA-Z0-9-]+", text)
            )
            sciebo_url = "https://rwth-aachen.sciebo.de"
            webdav_location = "/public.php/webdav/"
            for link in sciebo_links:
                logger.info(f"Found Sciebo Link: {link}")

                # get the download page
                try:
                    response = self.session.get(link)
                except Exception:
                    logger.exception(f"Failed to fetch Sciebo link {link}")
                    continue

                # parse html code
                soup = bs(response.text, features="lxml")

                # get the requesttoken
                requestToken = (
                    soup.head.get("data-requesttoken")
                    if soup.head is not None
                    else None
                )
                if not requestToken:
                    logger.warning(
                        "Sciebo: missing request token for link %s, skipping", link
                    )
                    continue
                logger.info(f"Sciebo request token: {requestToken}")

                # get the property value of the input tag with the name sharingToken
                sharing_input = soup.find("input", {"name": "sharingToken"})
                if not sharing_input or not sharing_input.get("value"):
                    logger.warning(
                        "Sciebo: missing sharingToken for link %s, skipping", link
                    )
                    continue
                sharingToken = sharing_input["value"]
                logger.info(f"Sciebo sharingToken: {sharingToken}")

                # get baseauthentication secret
                baseAuthSecret = base64.b64encode(
                    f"{sharingToken}:null".encode()
                ).decode()
                logger.info("Sciebo base auth secret derived")

                # get auth header
                auth_header = {
                    "Authorization": f"Basic {baseAuthSecret}",
                    "requesttoken": requestToken,
                }

                sciebo_root = parent_node.add_child(
                    f"sciebo-{sharingToken}", None, "Sciebo Folder"
                )
                if sciebo_root is None:
                    # Duplicate folder/link, nothing more to do here
                    continue

                # recursive function to get all files in the sciebo folder
                def get_sciebo_files(
                    href: str, parent_node: Node, sharingToken: str, auth_header: dict
                ):

                    # request the URL with the PROPFIND method and a body that
                    # also asks Sciebo/Nextcloud to include content checksums
                    # (oc:checksums) for each item. These checksums are stable
                    # content hashes (e.g. SHA1) and allow us to safely compare
                    # local files against the current remote content without
                    # relying on ETags.
                    propfind_body = """<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <d:getlastmodified/>
    <d:getetag/>
    <oc:checksums/>
  </d:prop>
</d:propfind>"""
                    headers = {
                        **auth_header,
                        "Depth": "1",
                        "Content-Type": "application/xml",
                    }
                    try:
                        propfind_response = self.session.request(
                            "PROPFIND",
                            sciebo_url + href,
                            headers=headers,
                            data=propfind_body,
                        )
                    except Exception:
                        logger.exception(
                            "Sciebo PROPFIND failed for href %s (share %s)",
                            href,
                            sharingToken,
                        )
                        return

                    if not (200 <= propfind_response.status_code < 300):
                        logger.warning(
                            "Sciebo PROPFIND returned status %s for href %s (share %s)",
                            propfind_response.status_code,
                            href,
                            sharingToken,
                        )
                        return

                    # parse the response
                    soup_xml = bs(propfind_response.text, features="xml")

                    for resp in soup_xml.find_all("d:response"):
                        # get the href of the response
                        href_tag = resp.find("d:href")
                        if href_tag is None or not href_tag.text:
                            continue
                        new_href = href_tag.text

                        if new_href == href:
                            logger.info(
                                "Sciebo: skipping %s because it is the current folder",
                                new_href,
                            )
                            continue

                        # Extract a stable content hash for this item. Prefer the
                        # SHA1 checksum from oc:checksums if available; fall back
                        # to the raw ETag otherwise.
                        etag_value = None
                        prop = resp.find("d:prop")
                        if prop is not None:
                            checksums_tag = prop.find("oc:checksums")
                            if checksums_tag is not None:
                                for cs in checksums_tag.find_all("oc:checksum"):
                                    text = (cs.text or "").strip()
                                    if text.upper().startswith("SHA1:"):
                                        etag_value = text.split(":", 1)[1]
                                        break

                            if etag_value is None:
                                etag_tag = prop.find("d:getetag")
                                if etag_tag and etag_tag.text:
                                    etag_value = etag_tag.text.strip()

                        logger.info(f"Sciebo response href: {new_href}")
                        # get the displayname of the response
                        displayname = (
                            new_href.split("/")[-2]
                            if new_href.endswith("/")
                            else new_href.split("/")[-1]
                        )
                        displayname = (
                            f"sciebo-{sharingToken}"
                            if displayname == "webdav"
                            else displayname
                        )

                        # check if the response is a folder
                        if new_href.endswith("/"):
                            # create a new node for the folder
                            folder_node = parent_node.add_child(
                                displayname, None, "Sciebo Folder", etag=etag_value
                            )
                            # recursive call to get all files in the folder
                            get_sciebo_files(
                                new_href, folder_node, sharingToken, auth_header
                            )
                        else:
                            # create a new node for the file
                            parent_node.add_child(
                                displayname,
                                None,
                                "Sciebo File",
                                url=sciebo_url + new_href,
                                additional_info=auth_header,
                                etag=etag_value,
                            )

                get_sciebo_files(
                    webdav_location, sciebo_root, sharingToken, auth_header
                )


def main():
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
        "--rootnodecachefile",
        default=None,
        help="set the location of a root node cache file",
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

    config["user"] = args.user or config.get("user")
    config["password"] = args.password or config.get("password")
    config["totp"] = args.totp or config.get("totp")
    config["totpsecret"] = args.totpsecret or config.get("totpsecret")
    config["cookie_file"] = args.cookiefile or config.get("cookie_file", "./session")
    config["root_node_cache_file"] = args.rootnodecachefile or config.get(
        "root_node_cache_file", "./cached_root_node"
    )
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
        args.secretservice if keyring else None
    ) or config.get("use_secret_service")
    config["secret_service_store_totp_secret"] = (
        args.secretservicetotpsecret if keyring else None
    ) or config.get("secret_service_store_totp_secret")
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
    config["exclude_files"] = config.get("exclude_files", [])
    config["updatefiles"] = args.updatefiles or config.get("update_files", False)
    config["update_files_conflict"] = args.updatefilesconflict or config.get(
        "update_files_conflict", "rename"
    )

    logging.basicConfig(level=args.loglevel)

    if pdfkit is None and config["used_modules"]["url"]["quiz"]:
        config["used_modules"]["url"]["quiz"] = False
        logger.warning("pdfkit is not installed. Quiz-PDFs are NOT generated")

    if not shutil.which("wkhtmltopdf") and config["used_modules"]["url"]["quiz"]:
        config["used_modules"]["url"]["quiz"] = False
        logger.warning(
            "You do not have wkhtmltopdf in your path. Quiz-PDFs are NOT generated"
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
        if smm._opencast_error_count >= 5:
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
